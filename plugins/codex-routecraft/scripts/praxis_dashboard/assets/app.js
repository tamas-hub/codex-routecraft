(() => {
  const text = value => value === null || value === undefined ? "—" : (typeof value === "object" ? JSON.stringify(value) : value);
  const ratio = value => value === null || value === undefined ? "—" : (value * 100).toFixed(1) + "%";
  const observedPercent = value => value === null || value === undefined ? "—" : value.toFixed(1) + "%";
  const metric = (label, value, note = "") => {
    const el = document.createElement("div"); el.className = "metric";
    const a = document.createElement("small"); a.textContent = label;
    const b = document.createElement("b"); b.textContent = text(value);
    el.append(a, b);
    if (note) { const c = document.createElement("small"); c.textContent = note; el.append(c); }
    return el;
  };
  const render = (id, items) => document.querySelector(id).replaceChildren(...items.map(item => metric(...item)));
  const mix = (name, data) => Object.entries(data?.values || {}).map(([label, count]) => [name + " " + label, (data.denominator ? ratio(count / data.denominator) : "—"), "OBSERVED / " + count + " / " + (data.denominator ?? 0)]);
  const showRuns = async transition => {
    const target = document.querySelector("#runs"); target.textContent = "読み込み中…";
    const params = new URLSearchParams({requested_model: transition.requested_model, actual_model: transition.actual_model, requested_reasoning: transition.requested_reasoning, actual_reasoning: transition.actual_reasoning});
    try {
      const response = await fetch("/api/praxis/v1/runs?" + params, {cache:"no-store"}); const payload = await response.json();
      const rows = (payload.runs || []).map(run => {
        const row = document.createElement("article"); row.className = "run";
        row.textContent = [run.timestamp || "—", run.task_class || "task class unavailable", run.run_id || run.event_id || "—", (run.requested_model || "—") + " / " + (run.requested_reasoning || "—") + " → " + (run.actual_model || "—") + " / " + (run.actual_reasoning || "—"), "source: " + (run.decision_source || "unknown"), "reason: " + (run.decision_reason || "unavailable"), "confidence: " + text(run.confidence), "tokens: " + text(run.tokens), "duration ms: " + text(run.duration_ms), "memory: " + text(run.memory_used)].join(" · ");
        return row;
      });
      target.replaceChildren(...rows);
      if (!rows.length) target.textContent = "該当する安全な実行記録はありません。";
    } catch (_) { target.textContent = "実行詳細を読み込めませんでした。"; }
  };
  const load = async () => {
    const status = document.querySelector("#status");
    try {
      const response = await fetch("/api/praxis/v1/snapshot", {cache:"no-store"}); const payload = await response.json(); const data = payload.data || {};
      status.textContent = payload.available ? "OBSERVED · ローカル読み取り専用" : (payload.code === "source_error" ? "データ源を安全に読み取れません。コンポーネント状態のみ表示します。" : "データ源は利用できません。コンポーネント状態のみ表示します。");
      const impact = data.routecraft_impact || {}, cache = data.platform_efficiency || {}, memory = data.memory_effect || {}, verification = data.verification || {}, versions = data.system_status?.component_versions || {}, health = data.system_status?.health || {};
      const system = [["System health", health.system, "OBSERVED / unobserved is unknown"], ["Collector", health.collector, "OBSERVED / unobserved is unknown"], ["Agents", health.agents, "OBSERVED / unobserved is unknown"], ["Devices", health.devices, "OBSERVED / unobserved is unknown"], ["Observed runs", data.execution?.observed_runs, "event telemetry"]];
      Object.entries(versions).forEach(([name, value]) => system.push([name, value.version, "version " + value.status + " · build " + text(value.build) + " · commit " + text(value.commit)]));
      render("#system", system);
      document.querySelector("#impact-note").textContent = "OBSERVED: RouteCraft " + (impact.attribution_mix?.routecraft ?? 0) + " / unknown " + (impact.unknown_attribution ?? 0) + " / non-RouteCraft " + (impact.excluded_non_routecraft ?? 0) + "。不足で除外 " + (impact.excluded_missing_fields ?? 0) + "。MEASURED は A/B 根拠が必要です。";
      const changes = impact.route_changes || {}, ultra = impact.sol_ultra || {};
      render("#impact", [
        ["経路変更", changes.changed, "OBSERVED / 分母 " + (changes.denominator ?? 0)],
        ["経路不変", changes.unchanged, "OBSERVED"],
        ["model family changed", changes.model_family_changed, "OBSERVED"],
        ["reasoning only changed", changes.reasoning_only_changed, "OBSERVED"],
        ["Sol Offload Rate", ratio(impact.sol_offload?.rate), "OBSERVED / " + (impact.sol_offload?.offloaded ?? 0) + " / " + (impact.sol_offload?.requested_sol_runs ?? 0)],
        ["Sol Executions Avoided", impact.sol_offload?.avoided_count, "OBSERVED"],
        ["Ultra Requests", ultra.requested, "OBSERVED"],
        ["Ultra Comparable", ultra.denominator, "OBSERVED / 不足で除外 " + (ultra.excluded ?? 0)],
        ["Ultra Optimization Rate", ratio(ultra.optimization_rate), "OBSERVED / classifications below"],
        ["Sol Ultra retained", ultra.classifications?.retained, "OBSERVED"],
        ["Sol Ultra reasoning reduced", ultra.classifications?.reasoning_reduced, "OBSERVED"],
        ["Sol Ultra Terra offload", ultra.classifications?.terra_offload, "OBSERVED"],
        ["Sol Ultra Luna offload", ultra.classifications?.luna_offload, "OBSERVED"],
        ["Sol Ultra other", ultra.classifications?.other, "OBSERVED"],
        ["Savings L1", impact.estimated_savings?.observed_avoided_count, "OBSERVED avoided count"],
        ["Savings L2", "unavailable", "counterfactual factors absent"],
        ["Savings L3", "unavailable", "A/B measurement absent"],
        ["Routing Efficiency", impact.routing_efficiency?.score ?? "withheld", impact.routing_efficiency?.help || ""],
        ["Retry Reduction", "unavailable", "counterfactual baseline absent"],
        ["Repeated Investigation Avoided", "unavailable", "counterfactual baseline absent"],
        ["Context Reduction", "unavailable", "counterfactual baseline absent"],
        ...mix("Requested model", impact.requested_model_mix), ...mix("Actual model", impact.actual_model_mix),
        ...mix("Requested reasoning", impact.requested_reasoning_mix), ...mix("Actual reasoning", impact.actual_reasoning_mix),
        ...((impact.why_routes_changed || []).map(row => ["Why: " + row.reason, row.runs, "OBSERVED / " + observedPercent(row.percent)]))
      ]);
      const ab = data.ab_basis || {};
      const pair = (label, field) => [label, text(ab.on?.[field]) + " / " + text(ab.off?.[field]), "paired observed " + (ab.paired_observed?.[field] ?? 0) + " · reduction not inferred"];
      render("#benchmark", [
        ["Comparison status", ab.status || "unavailable", ab.status === "measured" ? "MEASURED evidence groups observed" : "MEASURED unavailable"],
        ["Observed groups ON / OFF", (ab.observed_groups?.on ?? 0) + " / " + (ab.observed_groups?.off ?? 0), "v1 and unpaired remain observed only"],
        ["Paired runs ON / OFF", (ab.on?.runs ?? 0) + " / " + (ab.off?.runs ?? 0), (ab.basis || "both groups and evidence required") + " · excluded " + ((ab.excluded?.v1 ?? 0) + (ab.excluded?.unpaired ?? 0) + (ab.excluded?.duplicate ?? 0) + (ab.excluded?.missing_identity ?? 0))],
        pair("Execution time ms", "execution_time_ms"), pair("Total tokens", "total_tokens"),
        pair("Uncached input", "uncached_input_tokens"), pair("Cached input", "cached_input_tokens"),
        pair("Output tokens", "output_tokens"), pair("Reasoning tokens", "reasoning_tokens"),
        pair("Model calls", "model_calls"), pair("Tool calls", "tool_calls"), pair("File reads", "file_reads"),
        pair("Retries", "retry_count"), pair("Test result", "test_result"), pair("Final success", "final_success")
      ]);
      render("#execution", [["Observed runs", data.execution?.observed_runs, "deduplicated telemetry"], ["実行中", data.runtime?.running, "OBSERVED"], ["完了", data.runtime?.completed, "OBSERVED"], ["失敗", data.runtime?.failed, "OBSERVED"], ["トークン", data.execution?.tokens, "OBSERVED / deduplicated"], ["時間 ms", data.execution?.duration_ms, "OBSERVED / deduplicated"]]);
      render("#platform", [["入力 tokens", cache.input_tokens, "OBSERVED"], ["cached input", cache.cached_input_tokens, "OBSERVED"], ["prompt cache hit", ratio(cache.prompt_cache_hit_rate), cache.source || "insufficient evidence"]]);
      render("#memory", [["recall-assisted", memory.recall_assisted, "OBSERVED"], ["Useful Recall", memory.useful_recall ?? "unavailable", "no useful-recall evidence"], ["case reuse", memory.case_reuse, "OBSERVED"], ["rules applied", memory.rules_applied, "OBSERVED"], ["coverage-aware rate", ratio(memory.rate), memory.status || "insufficient evidence"]]);
      render("#verification", [["Default setting", verification.setting_default || "auto_min", "POLICY"], ["Normal tasks", verification.normal_tasks, "OBSERVED"], ["Special events", verification.special_tasks, "SEGMENTED"], ["Performed checks", verification.performed_checks, "OBSERVED"], ["Avoided checks", verification.avoided_checks, "OBSERVED"], ["Avoidance rate", ratio(verification.avoidance_rate), "efficiency only"], ["NONE / MIN", (verification.budgets?.none ?? 0) + " / " + (verification.budgets?.min ?? 0), "OBSERVED"], ["STRICT / RELEASE", (verification.budgets?.strict ?? 0) + " / " + (verification.budgets?.release ?? 0), "OBSERVED"], ["PASS", verification.statuses?.pass ?? 0, "OBSERVED"], ["SKIPPED / NOT REQUIRED", (verification.statuses?.skipped ?? 0) + " / " + (verification.statuses?.not_required ?? 0), "normal outcomes"]]);
      const body = document.querySelector("#matrix");
      body.replaceChildren(...(impact.transition_matrix || []).map(entry => {
        const tr = document.createElement("tr");
        [entry.summary, entry.runs, observedPercent(entry.percent), entry.tokens, entry.duration_ms].forEach(value => { const td = document.createElement("td"); td.textContent = text(value); tr.append(td); });
        const td = document.createElement("td"); const button = document.createElement("button"); button.type = "button"; button.textContent = "詳細"; button.setAttribute("aria-label", entry.summary + " の安全な実行詳細"); button.addEventListener("click", () => showRuns(entry)); td.append(button); tr.append(td); return tr;
      }));
    } catch (_) { status.textContent = "ダッシュボードの読み込みに失敗しました。"; }
  };
  load();
})();
