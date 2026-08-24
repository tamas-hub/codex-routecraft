"""Command-line interface for RouteCraft Memory Local."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

from . import IMPORTANCE_LEVELS, MEMORY_LOCAL_VERSION, MEMORY_TYPES, RUNTIME_VERSION
from .errors import RouteCraftLocalError
from .git_tools import inspect_git, rule_based_session_summary
from .loop_bridge import configure as configure_loop
from .loop_bridge import status as loop_status
from .packs import build_context_pack, build_handoff_pack
from .service import RouteCraftService
from .context_engine import compile_context


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _emit(data: Any, *, json_mode: bool, human: str | None = None) -> None:
    if json_mode:
        print(_json_text({"ok": True, "data": data}))
    elif human is not None:
        print(human)
    elif isinstance(data, str):
        print(data)
    else:
        print(_json_text(data))


def _default_summary_path(kind: str) -> Path:
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    return codex_home / "routecraft" / kind / "latest-summary.json"


def _write_summary(path: str | Path, value: Any) -> None:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".routecraft-tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    os.replace(temporary, target)


def _collector_config() -> dict[str, Any]:
    configured = os.environ.get("ROUTECRAFT_COLLECTOR_CONFIG")
    if configured:
        path = Path(configured).expanduser()
    elif os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        path = Path(os.environ["LOCALAPPDATA"]) / "RouteCraft Observatory Tray" / "observatory-tray.json"
    else:
        path = Path.home() / ".config" / "routecraft" / "collector.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _routecraft_plugin_registration_count() -> int | None:
    try:
        command = ["codex", "plugin", "list", "--json"]
        if os.name == "nt":
            # Python cannot launch the extensionless WindowsApps launcher directly;
            # use the npm shim through cmd.exe, matching the PowerShell command.
            command = ["cmd.exe", "/d", "/c", "codex.cmd", "plugin", "list", "--json"]
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=8,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    try:
        payload = json.loads(completed.stdout.lstrip("\ufeff"))
    except (TypeError, ValueError):
        return None
    installed = payload.get("installed") if isinstance(payload, dict) else None
    if not isinstance(installed, list):
        return None
    return sum(
        isinstance(plugin, dict) and plugin.get("pluginId") == "codex-routecraft@routecraft"
        for plugin in installed
    )


def _git_worktree_breakdown(source_root: Path) -> dict[str, Any]:
    """Separate tracked source state from intentional local context.

    RouteCraft's Context Compiler writes `.ccc/` as device-local evidence.  It
    must remain visible, but it must not turn an otherwise unchanged tracked
    checkout into a generic DIRTY result.
    """

    result: dict[str, Any] = {
        "available": False,
        "tracked_clean": False,
        "tracked_changes": 0,
        "conflicts": 0,
        "local_context_untracked": 0,
        "other_untracked": 0,
    }
    try:
        status = subprocess.run(
            ["git", "-C", str(source_root), "status", "--porcelain=v1", "--untracked-files=all"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=8,
        )
        conflicts = subprocess.run(
            ["git", "-C", str(source_root), "diff", "--name-only", "--diff-filter=U"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=8,
        )
    except (OSError, subprocess.SubprocessError):
        return result
    if status.returncode != 0 or conflicts.returncode != 0:
        return result
    tracked_changes = 0
    local_context = 0
    other_untracked = 0
    for line in status.stdout.splitlines():
        if len(line) < 3:
            continue
        if line.startswith("?? "):
            path = line[3:].strip().strip('"').replace("\\", "/")
            if path == ".ccc" or path.startswith(".ccc/"):
                local_context += 1
            else:
                other_untracked += 1
        else:
            tracked_changes += 1
    conflict_count = sum(bool(line.strip()) for line in conflicts.stdout.splitlines())
    return {
        "available": True,
        "tracked_clean": tracked_changes == 0 and conflict_count == 0,
        "tracked_changes": tracked_changes,
        "conflicts": conflict_count,
        "local_context_untracked": local_context,
        "other_untracked": other_untracked,
    }


def _real_benchmark_gate_ready(rows: Sequence[dict[str, Any]]) -> bool:
    """Require complete, condition-gated real evidence before Gate A passes.

    `acceptance_pass` in the v2 runner is false when the OFF-isolation or ON
    policy-marker/evaluator contract fails, so a 100% acceptance row proves
    both task acceptance and treatment validity without exporting raw markers.
    """
    required_modes = {"off", "on_memory_off", "on_recall", "full_memory"}
    required_metrics = {"task_success", "test_pass", "acceptance_pass", "total_tokens"}
    by_mode: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        mode = str(row.get("mode", ""))
        metric = str(row.get("metric", ""))
        if mode not in required_modes or metric not in required_metrics:
            continue
        if metric in by_mode.setdefault(mode, {}):
            return False
        by_mode[mode][metric] = row
    if set(by_mode) != required_modes:
        return False
    for mode in required_modes:
        metrics = by_mode[mode]
        if set(metrics) != required_metrics:
            return False
        sample_sizes = {int(row.get("sample_size", 0) or 0) for row in metrics.values()}
        case_counts = {int(row.get("case_count", 0) or 0) for row in metrics.values()}
        if len(sample_sizes) != 1 or min(sample_sizes) < 10 or len(case_counts) != 1 or min(case_counts) < 10:
            return False
        sample_size = next(iter(sample_sizes))
        for row in metrics.values():
            if row.get("evidence_status") != "measured" or row.get("confidence") not in {"medium", "high"}:
                return False
            if int(row.get("available_count", -1) or 0) != sample_size:
                return False
        for metric in ("task_success", "test_pass", "acceptance_pass"):
            if metrics[metric].get("success_rate") != 100.0:
                return False
        if metrics["total_tokens"].get("mean_value") is None:
            return False
    return True


def _unified_doctor(service: RouteCraftService) -> dict[str, Any]:
    from routecraft_collector import (
        _decision_counts,
        _device_id,
        benchmark_summary,
        configured_source_root,
        device_health,
        fixture_payload_v4,
        memory_metrics,
        _summary_rows,
        security_summary,
        utc_now,
        validate_v4,
    )

    local = service.doctor()
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    source_root = configured_source_root(codex_home) or Path(__file__).resolve().parents[4]
    git_breakdown = _git_worktree_breakdown(source_root)
    opaque_device = _device_id(codex_home)
    observed_at = utc_now()
    device = device_health(source_root, opaque_device, observed_at, codex_home)
    memory = memory_metrics(str(service.data_dir), opaque_device, observed_at, codex_home, source_root)
    decision = _decision_counts(codex_home, source_root)
    benchmark = benchmark_summary(opaque_device, observed_at, codex_home / "routecraft" / "benchmark" / "latest-summary.json")
    security = security_summary(opaque_device, observed_at, codex_home / "routecraft" / "security" / "latest-summary.json")
    registration_count = _routecraft_plugin_registration_count()
    collector_config = _collector_config()
    control_enabled = bool(collector_config.get("control_center_enabled")) or os.environ.get("CONTROL_CENTER_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
    endpoint = collector_config.get("telemetry_endpoint")
    token_file = collector_config.get("telemetry_token_file")
    control_configured = bool(
        control_enabled
        and isinstance(endpoint, str)
        and endpoint.startswith("https://")
        and isinstance(token_file, str)
        and Path(token_file).is_file()
    )
    plugin_ok = device["plugin_health"] == "healthy" and registration_count == 1
    hooks_ok = device["hook_health"] == "healthy"
    agents_ok = device["agents_healthy"] == device["agents_total"] == 6
    local_ok = bool(local.get("ok"))
    decision_ok = decision is not None
    collector_ok = validate_v4(fixture_payload_v4())
    benchmark_evidence = _summary_rows(codex_home / "routecraft" / "benchmark" / "real-d1-summary.json", "benchmark_metric_evidence", opaque_device)
    security_validation = _summary_rows(codex_home / "routecraft" / "security" / "validation-d1-summary.json", "security_validations", opaque_device)
    legacy_components = _summary_rows(codex_home / "routecraft" / "legacy" / "latest-d1-summary.json", "legacy_components", opaque_device)
    evidence_confidence = "LOW"
    if benchmark_evidence:
        confidences = {str(row.get("confidence", "low")) for row in benchmark_evidence}
        evidence_confidence = "HIGH" if confidences == {"high"} else "MEDIUM" if "low" not in confidences else "LOW"
    graph_engine_ok = False
    graph_schema_ok = False
    graph_health: dict[str, Any] = {
        "graph_mode": "off",
        "graph_schema": "unavailable",
        "state_store": "UNAVAILABLE",
        "checkpoint": "UNAVAILABLE",
        "resume": "UNAVAILABLE",
        "lane_registry": "UNAVAILABLE",
        "execution_boundary": "UNAVAILABLE",
        "trusted_evidence": "UNAVAILABLE",
        "policy_version": "UNAVAILABLE",
        "allowlist": [],
        "config": "UNAVAILABLE",
    }
    gate_checks: dict[str, bool] | None = None
    requested_graph_mode = os.environ.get("ROUTECRAFT_GRAPH_MODE", "observe").strip().lower() or "observe"
    effective_graph_mode = "off"
    try:
        import routecraft_execution_graph as graph
        import routecraft_graph_cli as durable_graph

        graph_health = durable_graph.doctor()
        graph_engine_ok = graph_health.get("graph_engine") == "OK" and all(
            graph.validate_primitive(item) for item in graph.GRAPH_PRIMITIVES
        )
        graph_schema_ok = graph_health.get("graph_schema") == "v1" and graph.GRAPH_SCHEMA_VERSION == 1
        security_fixture_ok = bool(security_validation) and all(row.get("status") == "passed" for row in security_validation)
        legacy_replacement_ok = bool(legacy_components) and all(
            row.get("replacement_health") == "healthy"
            and int(row.get("consecutive_healthy_cycles", 0) or 0) >= 3
            and row.get("missing_snapshots") == 0
            and row.get("duplicate_ingestions") == 0
            for row in legacy_components
        )
        gate_checks = {
            "real_model_benchmark_e2e": _real_benchmark_gate_ready(benchmark_evidence),
            "security_rule_fixture_validation": security_fixture_ok,
            "legacy_replacement_health": legacy_replacement_ok,
            "runtime_regression": plugin_ok and hooks_ok and agents_ok,
            "control_center_regression": False,
            "memory_regression": local_ok and decision_ok,
            "collector_regression": collector_ok,
        }
        gate = {"gate": "hardening_gate_a", "required_checks": gate_checks, "passed": all(gate_checks.values())}
        effective_graph_mode = str(graph.mode_gate(requested_graph_mode, gate)["effective_mode"])
    except (ImportError, ValueError, TypeError):
        effective_graph_mode = "off"
    control_state = "DISABLED" if not control_enabled else "CONFIGURED" if control_configured else "DEGRADED"
    overall = plugin_ok and hooks_ok and agents_ok and local_ok and decision_ok and collector_ok and (not control_enabled or control_configured)
    return {
        "ok": overall,
        "Core": "OK" if plugin_ok and hooks_ok and agents_ok else "DEGRADED",
        "Plugin": "OK" if plugin_ok else "DEGRADED",
        "Hooks": "OK" if hooks_ok else "DEGRADED",
        "Agents": f"{device['agents_healthy']}/{device['agents_total']}",
        "Memory Local": "OK" if local_ok else "DEGRADED",
        "Decision": "OK" if decision_ok else "UNAVAILABLE",
        "Collector": "OK" if collector_ok else "DEGRADED",
        "Git": "OK" if git_breakdown["tracked_clean"] else "DIRTY" if git_breakdown["available"] else "UNAVAILABLE",
        "Tracked files": "OK" if git_breakdown["tracked_clean"] else "DIRTY" if git_breakdown["available"] else "UNAVAILABLE",
        "Local context / untracked state": (
            f"{git_breakdown['local_context_untracked']} local context / {git_breakdown['other_untracked']} other"
            if git_breakdown["available"]
            else "UNAVAILABLE"
        ),
        "API": control_state,
        "Control": control_state,
        "Benchmark": "UNAVAILABLE" if benchmark["status"] == "unavailable" else str(benchmark["status"]).upper(),
        "Security": "UNAVAILABLE" if security["status"] == "unavailable" else str(security["status"]).upper(),
        "Graph Engine": "OK" if graph_engine_ok else "UNAVAILABLE",
        "Graph Mode": effective_graph_mode,
        "Graph Schema": graph_health["graph_schema"] if graph_schema_ok else "UNAVAILABLE",
        "Graph State Store": graph_health["state_store"],
        "Checkpoint": graph_health["checkpoint"],
        "Resume": graph_health["resume"],
        "Lane Registry": graph_health["lane_registry"],
        "Execution Boundary": graph_health["execution_boundary"],
        "Trusted Evidence": graph_health["trusted_evidence"],
        "Policy Version": graph_health["policy_version"],
        "Current Graph Allowlist": graph_health["allowlist"],
        "Benchmark Evidence": evidence_confidence,
        "Policy Evidence": evidence_confidence,
        "Security Coverage": (
            "UNAVAILABLE"
            if not security_validation
            else "MEASURED"
            if all(row.get("status") == "passed" and row.get("confidence") in {"medium", "high"} for row in security_validation)
            else "PARTIAL"
        ),
        "Legacy Components": "UNOBSERVED" if not legacy_components else "OBSERVED",
        "details": {
            "plugin_registrations": registration_count,
            "memory_projects": local.get("projects", 0),
            "memory_records": local.get("memories", 0),
            "decision_cases": int(decision.get("decision_cases", 0)) if decision else 0,
            "collector_schema": 4,
            "control_center_enabled": control_enabled,
            "hardening_gate_a": gate_checks,
            "graph": graph_health,
            "git": git_breakdown,
        },
    }


def _read_body(body: str | None, input_file: str | None) -> str:
    if body is not None and input_file is not None:
        raise RouteCraftLocalError("--body と --input-file は同時に指定できません。")
    if input_file:
        if input_file == "-":
            return sys.stdin.read()
        try:
            return Path(input_file).read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise RouteCraftLocalError(f"入力ファイルを読めません: {exc}") from exc
    if body == "-":
        return sys.stdin.read()
    if body is not None:
        return body
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return ""


def _csv(values: Sequence[str] | None) -> list[str]:
    output: list[str] = []
    for value in values or ():
        output.extend(item.strip() for item in value.split(",") if item.strip())
    return list(dict.fromkeys(output))


def _add_json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="機械可読な JSON で出力")


def _add_project_ref(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", required=True, help="プロジェクト ID または完全な名前")


def _add_search_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--type", action="append", choices=MEMORY_TYPES, dest="types")
    parser.add_argument("--tag", action="append", dest="tags")
    parser.add_argument("--importance", action="append", choices=IMPORTANCE_LEVELS)
    parser.add_argument("--from", dest="created_from", help="作成日時の下限 (ISO-8601)")
    parser.add_argument("--to", dest="created_to", help="作成日時の上限 (ISO-8601)")
    parser.add_argument("--file", dest="filename", help="関連ファイルの部分一致")
    parser.add_argument("--commit", help="関連コミットの部分一致")
    parser.add_argument("--active", choices=("yes", "no", "any"), default="yes")
    parser.add_argument("--verified", choices=("yes", "no", "any"), default="any")
    parser.add_argument("--limit", type=int, default=50)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="routecraft",
        description="昨日のAI開発の続きを、今日のAIへ正確に引き継ぐローカル記憶ツール",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"routecraft {RUNTIME_VERSION} (memory-local {MEMORY_LOCAL_VERSION})",
    )
    parser.add_argument("--data-dir", help="DB・backup・export の保存先（既定: ~/.routecraft-memory-local）")
    parser.add_argument("--json", dest="global_json", action="store_true", help="機械可読な JSON で出力")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="ローカルDBを初期化")
    _add_json_flag(init)

    project = commands.add_parser("project", help="プロジェクト管理")
    project_commands = project.add_subparsers(dest="project_command", required=True)
    project_add = project_commands.add_parser("add", help="プロジェクトを登録")
    project_add.add_argument("--name")
    project_add.add_argument("--repo", "--from-repo", dest="repo_path")
    project_add.add_argument("--remote-url", default="")
    project_add.add_argument("--agent", action="append")
    project_add.add_argument("--language", action="append")
    project_add.add_argument("--tag", action="append")
    project_add.add_argument("--description", default="")
    project_add.add_argument("--objective", default="")
    _add_json_flag(project_add)

    project_list = project_commands.add_parser("list", help="プロジェクト一覧")
    project_list.add_argument("--include-archived", action="store_true")
    _add_json_flag(project_list)

    project_show = project_commands.add_parser("show", help="プロジェクト詳細")
    _add_project_ref(project_show)
    _add_json_flag(project_show)

    project_rename = project_commands.add_parser("rename", help="名前を変更")
    _add_project_ref(project_rename)
    project_rename.add_argument("--name", required=True)
    _add_json_flag(project_rename)

    project_edit = project_commands.add_parser("edit", help="プロジェクト情報を編集")
    _add_project_ref(project_edit)
    project_edit.add_argument("--repo")
    project_edit.add_argument("--remote-url")
    project_edit.add_argument("--agent", action="append")
    project_edit.add_argument("--language", action="append")
    project_edit.add_argument("--tag", action="append")
    project_edit.add_argument("--description")
    project_edit.add_argument("--objective")
    _add_json_flag(project_edit)

    project_archive = project_commands.add_parser("archive", help="アーカイブ状態を変更")
    _add_project_ref(project_archive)
    project_archive.add_argument("--undo", action="store_true", help="アーカイブを解除")
    _add_json_flag(project_archive)

    project_delete = project_commands.add_parser("delete", help="確認付きでプロジェクトを削除")
    _add_project_ref(project_delete)
    project_delete.add_argument("--confirm", required=True, help="対象のプロジェクト ID を完全入力")
    _add_json_flag(project_delete)

    project_backup = project_commands.add_parser("backup", help="プロジェクト持ち運びパッケージを作成")
    _add_project_ref(project_backup)
    project_backup.add_argument("--output", required=True)
    project_backup.add_argument("--folder", action="store_true", help="ZIPではなくフォルダを作成")
    _add_json_flag(project_backup)

    project_restore = project_commands.add_parser("restore", help="プロジェクトパッケージを取り込み")
    project_restore.add_argument("--input", required=True)
    project_restore.add_argument("--conflict", choices=("detect", "skip"), default="detect")
    _add_json_flag(project_restore)

    memory = commands.add_parser("memory", help="構造化メモリ管理")
    memory_commands = memory.add_subparsers(dest="memory_command", required=True)
    memory_add = memory_commands.add_parser("add", help="メモリを登録")
    _add_project_ref(memory_add)
    memory_add.add_argument("--type", required=True, choices=MEMORY_TYPES, dest="memory_type")
    memory_add.add_argument("--title", required=True)
    memory_add.add_argument("--body")
    memory_add.add_argument("--input-file")
    memory_add.add_argument("--importance", choices=IMPORTANCE_LEVELS, default="medium")
    memory_add.add_argument("--tag", action="append")
    memory_add.add_argument("--source", default="cli")
    memory_add.add_argument("--file", action="append", dest="related_files")
    memory_add.add_argument("--commit", action="append", dest="related_commits")
    memory_add.add_argument("--verified", action="store_true")
    _add_json_flag(memory_add)

    memory_list = memory_commands.add_parser("list", help="メモリ一覧")
    memory_list.add_argument("--project")
    memory_list.add_argument("--include-inactive", action="store_true")
    memory_list.add_argument("--type", action="append", choices=MEMORY_TYPES, dest="types")
    memory_list.add_argument("--importance", action="append", choices=IMPORTANCE_LEVELS)
    memory_list.add_argument("--limit", type=int, default=100)
    memory_list.add_argument("--offset", type=int, default=0)
    _add_json_flag(memory_list)

    memory_show = memory_commands.add_parser("show", help="メモリ詳細")
    memory_show.add_argument("--id", required=True)
    _add_json_flag(memory_show)

    memory_edit = memory_commands.add_parser("edit", help="メモリを編集")
    memory_edit.add_argument("--id", required=True)
    memory_edit.add_argument("--type", choices=MEMORY_TYPES, dest="memory_type")
    memory_edit.add_argument("--title")
    memory_edit.add_argument("--body")
    memory_edit.add_argument("--input-file")
    memory_edit.add_argument("--importance", choices=IMPORTANCE_LEVELS)
    memory_edit.add_argument("--tag", action="append")
    memory_edit.add_argument("--file", action="append", dest="related_files")
    memory_edit.add_argument("--commit", action="append", dest="related_commits")
    memory_edit.add_argument("--active", choices=("yes", "no"))
    memory_edit.add_argument("--verified", choices=("yes", "no"))
    _add_json_flag(memory_edit)

    memory_delete = memory_commands.add_parser("delete", help="確認付きでメモリを削除")
    memory_delete.add_argument("--id", required=True)
    memory_delete.add_argument("--confirm", required=True, help="対象のメモリ ID を完全入力")
    _add_json_flag(memory_delete)

    memory_search = memory_commands.add_parser("search", help="ローカル全文検索")
    memory_search.add_argument("query", nargs="?", default="")
    memory_search.add_argument("--project")
    _add_search_filters(memory_search)
    _add_json_flag(memory_search)

    memory_import = memory_commands.add_parser("import", help="Markdown / JSON / JSONL / 既存Storeを取り込み")
    _add_project_ref(memory_import)
    memory_import.add_argument("--input", required=True)
    memory_import.add_argument("--format", choices=("auto", "markdown", "json", "jsonl", "routecraft"), default="auto")
    _add_json_flag(memory_import)

    memory_export = memory_commands.add_parser("export", help="メモリを書き出し")
    memory_export.add_argument("--project")
    memory_export.add_argument("--format", choices=("json", "jsonl", "markdown"), default="jsonl")
    memory_export.add_argument("--output", required=True)
    memory_export.add_argument("--safe", action="store_true", help="秘密情報と端末固有pathを除外")
    _add_json_flag(memory_export)

    context = commands.add_parser("context", help="Context Pack")
    context_commands = context.add_subparsers(dest="context_command", required=True)
    context_build = context_commands.add_parser("build", help="Context Packを生成")
    _add_project_ref(context_build)
    context_build.add_argument("--format", choices=("markdown", "text", "json"), default="markdown")
    context_build.add_argument("--profile", choices=("compact", "standard", "full"), default="standard")
    context_build.add_argument("--max-chars", type=int)
    context_build.add_argument("--max-tokens", type=int)
    context_build.add_argument("--output")
    _add_json_flag(context_build)
    context_engine = context_commands.add_parser("engine", help="Context Engine adapter")
    _add_project_ref(context_engine)
    context_engine.add_argument("--format", choices=("markdown", "text", "json"), default="markdown")
    context_engine.add_argument("--profile", choices=("compact", "standard", "full"), default="standard")
    context_engine.add_argument("--max-chars", type=int)
    context_engine.add_argument("--max-tokens", type=int)
    _add_json_flag(context_engine)

    handoff = commands.add_parser("handoff", help="AI間Handoff Pack")
    handoff_commands = handoff.add_subparsers(dest="handoff_command", required=True)
    handoff_build = handoff_commands.add_parser("build", help="Handoff Packを生成")
    _add_project_ref(handoff_build)
    handoff_build.add_argument("--output", required=True)
    handoff_build.add_argument("--zip", action="store_true")
    _add_json_flag(handoff_build)

    git_command = commands.add_parser("git", help="Git情報（読み取り専用）")
    git_commands = git_command.add_subparsers(dest="git_command", required=True)
    git_status = git_commands.add_parser("status", help="Git状態を取得")
    _add_project_ref(git_status)
    git_status.add_argument("--recent", type=int, default=10)
    _add_json_flag(git_status)

    session = commands.add_parser("session", help="セッション要約")
    session_commands = session.add_subparsers(dest="session_command", required=True)
    session_summary = session_commands.add_parser("summarize", help="Git差分からルールベース要約")
    _add_project_ref(session_summary)
    session_summary.add_argument("--save", action="store_true")
    session_summary.add_argument("--importance", choices=IMPORTANCE_LEVELS, default="medium")
    _add_json_flag(session_summary)

    loop = commands.add_parser("loop", help="Codex RouteCraft Loopとのローカル連携")
    loop_commands = loop.add_subparsers(dest="loop_command", required=True)
    loop_status_command = loop_commands.add_parser("status", help="Loop連携設定を表示")
    _add_json_flag(loop_status_command)
    loop_configure = loop_commands.add_parser("configure", help="Loop連携を有効化または無効化")
    loop_mode = loop_configure.add_mutually_exclusive_group(required=True)
    loop_mode.add_argument("--enable", dest="loop_enabled", action="store_true")
    loop_mode.add_argument("--disable", dest="loop_enabled", action="store_false")
    loop_configure.add_argument("--auto-context", action=argparse.BooleanOptionalAction, default=None)
    loop_configure.add_argument("--auto-session-summary", action=argparse.BooleanOptionalAction, default=None)
    loop_configure.add_argument("--context-profile", choices=("compact", "standard", "full"))
    loop_configure.add_argument("--max-context-chars", type=int)
    _add_json_flag(loop_configure)

    status = commands.add_parser("status", help="ローカル状態を表示")
    _add_json_flag(status)
    doctor = commands.add_parser("doctor", help="RouteCraft 全体の統合診断")
    doctor.add_argument("--scope", choices=("local", "health", "all"), default="health")
    _add_json_flag(doctor)

    collector = commands.add_parser("collector", help="Unified Collector (local only)")
    collector_commands = collector.add_subparsers(dest="collector_command", required=True)
    collector_collect = collector_commands.add_parser("collect", help="schema v4 privacy-safe payload を生成")
    collector_collect.add_argument("--sessions-dir")
    collector_collect.add_argument("--since-days", type=int, default=30)
    _add_json_flag(collector_collect)

    graph = commands.add_parser("graph", help="Execution Graph deterministic engine")
    graph_commands = graph.add_subparsers(dest="graph_command", required=True)
    graph_mode = graph_commands.add_parser("mode", help="[legacy 0.6 adapter] off/observe only; enforce is never authorized")
    graph_mode.add_argument("--mode", choices=("off", "observe", "enforce"), default=os.environ.get("ROUTECRAFT_GRAPH_MODE", "observe"))
    graph_mode.add_argument("--hardening-gate")
    _add_json_flag(graph_mode)
    graph_create = graph_commands.add_parser("create", help="[deprecated legacy] units JSONのobserve shadowを生成")
    graph_create.add_argument("--input", required=True)
    graph_create.add_argument("--state-output", required=True)
    graph_create.add_argument("--mode", choices=("off", "observe", "enforce"), default=os.environ.get("ROUTECRAFT_GRAPH_MODE", "observe"))
    graph_create.add_argument("--hardening-gate")
    graph_create.add_argument("--summary-output", default=str(_default_summary_path("graph").with_name("latest-d1-summary.json")))
    graph_create.add_argument("--no-summary", action="store_true")
    _add_json_flag(graph_create)
    graph_plan = graph_commands.add_parser("plan", help="Graph IR v1をcompileし、専用SQLiteへcheckpoint")
    graph_plan.add_argument("--input", required=True)
    graph_plan.add_argument("--config")
    graph_plan.add_argument("--store")
    graph_plan.add_argument("--mode", choices=("off", "observe", "enforce"))
    _add_json_flag(graph_plan)
    for name in ("validate", "ready", "shadow", "summary"):
        item = graph_commands.add_parser(name)
        item.add_argument("--input", required=True)
        if name == "validate":
            item.add_argument("--config")
        if name == "ready":
            item.add_argument("--max-parallelism", type=int, default=3)
        if name == "shadow":
            item.add_argument("--predictions", required=True)
        if name in {"shadow", "summary"}:
            item.add_argument("--summary-output", default=str(_default_summary_path("graph").with_name("latest-d1-summary.json")))
            item.add_argument("--no-summary", action="store_true")
        _add_json_flag(item)
    graph_run = graph_commands.add_parser("run", help="Ready nodeのclaimまたはstructured resultをdurable記録")
    graph_run.add_argument("--graph-id", required=True)
    graph_run.add_argument("--node")
    graph_run.add_argument("--result")
    graph_run.add_argument("--evidence")
    graph_run.add_argument("--usage", help="nullable実測値を持つAttempt usage JSON")
    graph_run.add_argument("--gate-result", choices=("PASS", "FAIL", "INCONCLUSIVE"), default="PASS")
    graph_run.add_argument("--failure")
    graph_run.add_argument("--retry", action="store_true")
    graph_run.add_argument("--config")
    graph_run.add_argument("--store")
    _add_json_flag(graph_run)
    graph_approve = graph_commands.add_parser("approve", help="Human Approval Nodeをcurrent input hashへ明示承認")
    graph_approve.add_argument("--graph-id", required=True)
    graph_approve.add_argument("--node", required=True)
    graph_approve.add_argument(
        "--confirm",
        required=True,
        help="<graph_id>:<node_id>:<input_hash>:<operation_hash> の完全一致",
    )
    graph_approve.add_argument("--actor-ref", required=True)
    graph_approve.add_argument("--operation", required=True, help="kind/target_scope/parameters_hashを持つJSON")
    graph_approve.add_argument("--evidence", required=True)
    graph_approve.add_argument("--usage", required=True)
    graph_approve.add_argument("--config")
    graph_approve.add_argument("--store")
    _add_json_flag(graph_approve)
    for name in ("resume", "status", "cancel"):
        item = graph_commands.add_parser(name)
        item.add_argument("--graph-id", required=True)
        item.add_argument("--config")
        item.add_argument("--store")
        if name == "status":
            item.add_argument("--include-graph", action="store_true")
        if name == "cancel":
            item.add_argument("--confirm", required=True)
        _add_json_flag(item)
    graph_export = graph_commands.add_parser("export", help="Graph stateとprivacy-safe summaryをlocal export")
    graph_export.add_argument("--graph-id", required=True)
    graph_export.add_argument("--output", required=True)
    graph_export.add_argument("--config")
    graph_export.add_argument("--store")
    _add_json_flag(graph_export)

    policy = commands.add_parser("policy", help="Production policyとhuman-gated candidateを表示")
    policy_commands = policy.add_subparsers(dest="policy_command", required=True)
    for name in ("status", "candidates"):
        item = policy_commands.add_parser(name)
        item.add_argument("--config")
        item.add_argument("--store")
        if name == "candidates":
            item.add_argument("--include-special-events", action="store_true")
        _add_json_flag(item)

    agents = commands.add_parser("agents", help="AGENTS optimizer")
    agents_commands = agents.add_subparsers(dest="agents_command", required=True)
    for name in ("analyze", "preview", "apply"):
        item = agents_commands.add_parser(name)
        item.add_argument("--path", default="AGENTS.md")
        if name == "apply":
            item.add_argument("--confirm", required=True)
        _add_json_flag(item)

    harden = commands.add_parser("security", help="Security Hardener")
    harden_commands = harden.add_subparsers(dest="security_command", required=True)
    for name in ("analyze", "preview", "apply"):
        item = harden_commands.add_parser(name)
        item.add_argument("--config", required=True)
        item.add_argument("--source-root")
        if name in {"analyze", "preview"}:
            item.add_argument("--baseline")
        if name == "analyze":
            item.add_argument("--summary-output", default=str(_default_summary_path("security")))
            item.add_argument("--no-summary", action="store_true")
        if name == "apply":
            item.add_argument("--confirm", required=True)
        _add_json_flag(item)

    benchmark = commands.add_parser("benchmark", help="deterministic local Benchmark Lab")
    benchmark.add_argument("--fixture", default=str(Path(__file__).resolve().parents[4] / "samples" / "benchmark-lab-fixture.json"))
    benchmark.add_argument("--observed")
    benchmark.add_argument("--summary-output", default=str(_default_summary_path("benchmark")))
    benchmark.add_argument("--no-summary", action="store_true")
    benchmark.add_argument("--real-preflight", action="store_true", help="modelを呼ばずReal Benchmark sandbox/plugin境界を検証")
    benchmark.add_argument("--codex-bin", default="codex")
    _add_json_flag(benchmark)

    update = commands.add_parser("update", help="既存device bootstrapへ明示委譲")
    update.add_argument("--apply", action="store_true")
    update.add_argument("--source-dir")
    update.add_argument("--memory-dir")
    update.add_argument("--source-branch")
    update.add_argument("--memory-branch")
    update.add_argument("--source-remote")
    update.add_argument("--memory-remote")
    update.add_argument("--allow-first-device", action="store_true")
    update.add_argument("--enable-project-source-guard", action="store_true")
    update.add_argument("--github-owner")
    _add_json_flag(update)

    migrate = commands.add_parser("migrate", help="既存Local Runtime migrationへ明示委譲")
    migrate_commands = migrate.add_subparsers(dest="migrate_command", required=True)
    migrate_db = migrate_commands.add_parser("local-db")
    migrate_db.add_argument("--confirm", required=True)
    _add_json_flag(migrate_db)
    migrate_store = migrate_commands.add_parser("decision-store")
    migrate_store.add_argument("--project", required=True)
    migrate_store.add_argument("--input", required=True)
    migrate_store.add_argument("--confirm", required=True)
    _add_json_flag(migrate_store)
    migrate_endpoint = migrate_commands.add_parser("endpoint")
    migrate_endpoint.add_argument("--config", required=True)
    migrate_endpoint.add_argument("--old-url", required=True)
    migrate_endpoint.add_argument("--new-url", required=True)
    migrate_endpoint.add_argument("--apply", action="store_true")
    migrate_endpoint.add_argument("--confirm")
    _add_json_flag(migrate_endpoint)
    migrate_graph = migrate_commands.add_parser("graph-config", help="Graph config v1のdry-runまたはatomic作成")
    migrate_graph.add_argument("--config")
    migrate_graph.add_argument("--existing", help="既存設定JSON（未指定は0.6 defaultから移行）")
    migrate_graph.add_argument("--apply", action="store_true")
    migrate_graph.add_argument("--confirm")
    _add_json_flag(migrate_graph)

    backup = commands.add_parser("backup", help="DBバックアップを作成")
    backup.add_argument("--output")
    _add_json_flag(backup)
    restore = commands.add_parser("restore", help="DBバックアップから安全に復元")
    restore.add_argument("--input", required=True)
    restore.add_argument("--confirm", required=True, help="RESTORE と完全入力")
    _add_json_flag(restore)

    export = commands.add_parser("export", help="全体またはproject単位を書き出し")
    export.add_argument("--project")
    export.add_argument("--format", choices=("json", "jsonl", "markdown"), default="jsonl")
    export.add_argument("--output", required=True)
    export.add_argument("--safe", action="store_true")
    _add_json_flag(export)

    import_command = commands.add_parser("import", help="project packageを取り込み")
    import_command.add_argument("--input", required=True)
    import_command.add_argument("--conflict", choices=("detect", "skip"), default="detect")
    _add_json_flag(import_command)

    ui = commands.add_parser("ui", help="日本語ローカルWeb UIを起動")
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument("--no-browser", action="store_true")
    _add_json_flag(ui)

    return parser


def _bool_filter(value: str) -> bool | None:
    return None if value == "any" else value == "yes"


def _handle(args: argparse.Namespace, service: RouteCraftService, json_mode: bool) -> int:
    command = args.command
    if command == "init":
        result = service.initialize()
        _emit(result, json_mode=json_mode, human=f"初期化しました: {result.get('database', result.get('db_path', ''))}")
        return 0

    if command == "loop":
        if args.loop_command == "status":
            result = loop_status()
            _emit(result, json_mode=json_mode)
            return 0
        result = configure_loop(
            enabled=args.loop_enabled,
            data_dir=args.data_dir,
            auto_context=args.auto_context,
            auto_session_summary=args.auto_session_summary,
            context_profile=args.context_profile,
            max_context_chars=args.max_context_chars,
        )
        if result["enabled"]:
            RouteCraftService(result["data_dir"]).initialize()
        state = "enabled" if result["enabled"] else "disabled"
        _emit(result, json_mode=json_mode, human=f"RouteCraft Memory Local Loop integration: {state}")
        return 0

    service.initialize()

    if command == "project":
        sub = args.project_command
        if sub == "add":
            repo = str(Path(args.repo_path).expanduser().resolve()) if args.repo_path else ""
            git = inspect_git(repo) if repo else {}
            name = args.name or (Path(repo).name if repo else "")
            if not name:
                raise RouteCraftLocalError("--name または --repo が必要です。")
            result = service.add_project(
                name=name,
                repo_path=repo,
                git_remote_url=args.remote_url or str(git.get("remote_url", "")),
                ai_agents=_csv(args.agent),
                languages=_csv(args.language),
                tags=_csv(args.tag),
                description=args.description,
                current_objective=args.objective,
            )
            _emit(result, json_mode=json_mode, human=f"プロジェクトを登録しました: {result['name']} ({result['id']})")
        elif sub == "list":
            result = service.list_projects(include_archived=args.include_archived)
            lines = [f"{item['id']}\t{item['name']}\t{'archived' if item.get('archived') else 'active'}" for item in result]
            _emit(result, json_mode=json_mode, human="\n".join(lines) or "プロジェクトはありません。")
        elif sub == "show":
            result = service.get_project(args.project)
            _emit(result, json_mode=json_mode)
        elif sub == "rename":
            result = service.update_project(args.project, name=args.name)
            _emit(result, json_mode=json_mode, human=f"名前を変更しました: {result['name']}")
        elif sub == "edit":
            changes: dict[str, Any] = {}
            mapping = {
                "repo_path": args.repo,
                "git_remote_url": args.remote_url,
                "description": args.description,
                "current_objective": args.objective,
            }
            changes.update({key: value for key, value in mapping.items() if value is not None})
            if args.agent is not None:
                changes["ai_agents"] = _csv(args.agent)
            if args.language is not None:
                changes["languages"] = _csv(args.language)
            if args.tag is not None:
                changes["tags"] = _csv(args.tag)
            result = service.update_project(args.project, **changes)
            _emit(result, json_mode=json_mode, human=f"プロジェクトを更新しました: {result['id']}")
        elif sub == "archive":
            result = service.archive_project(args.project, archived=not args.undo)
            _emit(result, json_mode=json_mode, human=f"アーカイブ状態: {bool(result.get('archived'))}")
        elif sub == "delete":
            result = service.delete_project(args.project, args.confirm)
            _emit(result, json_mode=json_mode, human=f"削除しました。安全コピー: {result.get('safety_copy', '')}")
        elif sub == "backup":
            result = service.export_project_package(args.project, args.output, as_zip=not args.folder)
            _emit(result, json_mode=json_mode, human=f"作成しました: {result.get('output', args.output)}")
        elif sub == "restore":
            result = service.import_project_package(args.input, conflict=args.conflict)
            _emit(result, json_mode=json_mode)
        return 0

    if command == "memory":
        sub = args.memory_command
        if sub == "add":
            body = _read_body(args.body, args.input_file)
            if not body.strip():
                raise RouteCraftLocalError("本文が空です。--body、--input-file、またはstdinを指定してください。")
            result = service.add_memory(
                args.project,
                args.memory_type,
                args.title,
                body,
                importance=args.importance,
                tags=_csv(args.tag),
                source=args.source,
                related_files=_csv(args.related_files),
                related_commits=_csv(args.related_commits),
                verified=args.verified,
            )
            warning = f" / masking: {', '.join(result.get('warnings', []))}" if result.get("warnings") else ""
            _emit(result, json_mode=json_mode, human=f"メモリを登録しました: {result['id']}{warning}")
        elif sub == "list":
            result = service.list_memories(
                args.project,
                limit=args.limit,
                offset=args.offset,
                include_inactive=args.include_inactive,
                types=_csv(args.types),
                importance=_csv(args.importance),
            )
            lines = [
                f"{item['id']}\t{item.get('memory_type', item.get('type', 'note'))}\t{item['importance']}\t{item['title']}"
                for item in result
            ]
            _emit(result, json_mode=json_mode, human="\n".join(lines) or "メモリはありません。")
        elif sub == "show":
            _emit(service.get_memory(args.id), json_mode=json_mode)
        elif sub == "edit":
            changes: dict[str, Any] = {}
            if args.memory_type is not None:
                changes["memory_type"] = args.memory_type
            if args.title is not None:
                changes["title"] = args.title
            if args.body is not None or args.input_file is not None:
                changes["body"] = _read_body(args.body, args.input_file)
            if args.importance is not None:
                changes["importance"] = args.importance
            if args.tag is not None:
                changes["tags"] = _csv(args.tag)
            if args.related_files is not None:
                changes["related_files"] = _csv(args.related_files)
            if args.related_commits is not None:
                changes["related_commits"] = _csv(args.related_commits)
            if args.active is not None:
                changes["active"] = args.active == "yes"
            if args.verified is not None:
                changes["verified"] = args.verified == "yes"
            result = service.update_memory(args.id, **changes)
            _emit(result, json_mode=json_mode, human=f"更新しました: {result['id']}")
        elif sub == "delete":
            result = service.delete_memory(args.id, args.confirm)
            _emit(result, json_mode=json_mode, human=f"削除しました: {args.id}")
        elif sub == "search":
            result = service.search_memories(
                args.project,
                args.query,
                types=_csv(args.types),
                tags=_csv(args.tags),
                importance=_csv(args.importance),
                created_from=args.created_from,
                created_to=args.created_to,
                filename=args.filename,
                commit=args.commit,
                active=_bool_filter(args.active),
                verified=_bool_filter(args.verified),
                limit=args.limit,
            )
            lines = [
                f"{item.get('relevance', 0):.2f}\t{item.get('memory_type', item.get('type', 'note'))}\t{item['importance']}\t{item['title']}"
                for item in result
            ]
            _emit(result, json_mode=json_mode, human="\n".join(lines) or "該当するメモリはありません。")
        elif sub == "import":
            if args.format == "routecraft":
                result = service.import_routecraft_store(args.project, args.input)
            else:
                result = service.import_file(args.project, args.input, format=args.format)
            _emit(result, json_mode=json_mode)
        elif sub == "export":
            result = service.export_memories(args.project, fmt=args.format, output=args.output, safe=args.safe)
            _emit(result, json_mode=json_mode, human=f"書き出しました: {result.get('output', args.output)}")
        return 0

    if command == "context":
        if args.context_command == "engine":
            result = compile_context(
                service,
                args.project,
                format=args.format,
                profile=args.profile,
                max_chars=args.max_chars,
                max_tokens=args.max_tokens,
            )
            _emit(result, json_mode=json_mode)
            return 0
        result = build_context_pack(
            service,
            args.project,
            format=args.format,
            profile=args.profile,
            max_chars=args.max_chars,
            max_tokens=args.max_tokens,
        )
        if args.output:
            target = Path(args.output).expanduser()
            if target.exists():
                raise RouteCraftLocalError("Context Pack の出力先は既に存在します。別のパスを指定してください。")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(result["content"]), encoding="utf-8", newline="\n")
            result["path"] = str(target.resolve())
            _emit(result, json_mode=json_mode, human=f"Context Pack: {result['path']} ({result['char_count']} chars / 約{result['estimated_tokens']} tokens)")
        else:
            _emit(result, json_mode=json_mode, human=str(result["content"]))
        return 0

    if command == "handoff":
        result = build_handoff_pack(service, args.project, args.output, as_zip=args.zip)
        destination = result.get("zip") or result.get("folder") or args.output
        _emit(result, json_mode=json_mode, human=f"Handoff Pack: {destination}")
        return 0

    if command == "git":
        project = service.get_project(args.project)
        result = inspect_git(project.get("repo_path", ""), recent_limit=args.recent)
        _emit(result, json_mode=json_mode)
        return 0

    if command == "session":
        project = service.get_project(args.project)
        summary = rule_based_session_summary(project.get("repo_path", ""))
        result: dict[str, Any] = {"summary": summary}
        if args.save:
            result["memory"] = service.add_memory(
                project["id"],
                "session_summary",
                summary["title"],
                summary["body"],
                importance=args.importance,
                tags=("git", "session"),
                source="git-rule-based",
                related_files=summary.get("related_files", ()),
                related_commits=summary.get("related_commits", ()),
            )
        _emit(result, json_mode=json_mode)
        return 0

    if command == "status":
        result = service.doctor()
        _emit(result, json_mode=json_mode)
        return 0
    if command == "doctor":
        result = service.doctor() if args.scope == "local" else _unified_doctor(service)
        _emit(result, json_mode=json_mode)
        return 0 if result.get("ok", True) else 1
    if command == "collector":
        from routecraft_collector import collect_v4

        source_root = Path(__file__).resolve().parents[4]
        sessions = Path(args.sessions_dir) if args.sessions_dir else None
        result = collect_v4(source_root=source_root, data_dir=str(service.data_dir), sessions_dir=sessions, since_days=args.since_days)
        _emit(result, json_mode=json_mode)
        return 0
    if command == "graph":
        import routecraft_execution_graph as graph
        import routecraft_graph_cli as durable_graph

        if args.graph_command == "mode":
            if args.mode == "enforce":
                raise RouteCraftLocalError(
                    "ENFORCE_BOUNDARY_UNAVAILABLE: legacy graph mode cannot authorize enforce; use Graph IR v1 through graph plan with an injected trusted host boundary."
                )
            gate = json.loads(Path(args.hardening_gate).read_text(encoding="utf-8")) if args.hardening_gate else None
            result = graph.mode_gate(args.mode, gate)
        elif args.graph_command == "plan":
            result = durable_graph.plan(
                args.input,
                config_path=args.config,
                store_path=args.store,
                data_dir=service.data_dir,
                mode=args.mode,
            )
        elif args.graph_command == "create":
            if args.mode == "enforce":
                raise RouteCraftLocalError(
                    "ENFORCE_BOUNDARY_UNAVAILABLE: deprecated graph create supports observe/off only; use Graph IR v1 graph plan."
                )
            plan = json.loads(Path(args.input).read_text(encoding="utf-8"))
            if not isinstance(plan, dict):
                raise RouteCraftLocalError("Graph plan input must be a JSON object.")
            state_target = Path(args.state_output).expanduser()
            if state_target.exists():
                raise RouteCraftLocalError("Graph state output already exists. Use a new caller-owned path.")
            gate = json.loads(Path(args.hardening_gate).read_text(encoding="utf-8")) if args.hardening_gate else None
            state = graph.create_graph(
                str(plan.get("graph_id", "")),
                str(plan.get("task_class", "")),
                plan.get("units", plan.get("nodes")),
                edges=plan.get("edges"),
                constraints=plan.get("constraints"),
                limits=plan.get("limits"),
                mode=args.mode,
                hardening_gate=gate,
            )
            graph.validate_graph_or_raise(state)
            _write_summary(state_target, state)
            summary = graph.to_d1_summary(state)
            if not args.no_summary:
                _write_summary(args.summary_output, summary)
            result = {
                "valid": True,
                "graph_schema_version": graph.GRAPH_SCHEMA_VERSION,
                "mode": state["mode"],
                "requested_mode": state["requested_mode"],
                "node_count": len(state["nodes"]),
                "edge_count": len(state["edges"]),
                "state_saved": True,
                "control_center_summary_saved": not args.no_summary,
            }
        else:
            if args.graph_command == "validate":
                state = json.loads(Path(args.input).read_text(encoding="utf-8-sig"))
                if isinstance(state, dict) and "graph_schema_version" in state:
                    result = durable_graph.validate_file(args.input, config_path=args.config)
                else:
                    valid = graph.validate_graph(state)
                    result = {"valid": valid, "graph_schema_version": graph.GRAPH_SCHEMA_VERSION}
            elif args.graph_command == "run":
                result = durable_graph.run(
                    args.graph_id,
                    config_path=args.config,
                    store_path=args.store,
                    data_dir=service.data_dir,
                    node_id=args.node,
                    result_path=args.result,
                    evidence_path=args.evidence,
                    usage_path=args.usage,
                    gate_result=args.gate_result,
                    failure=args.failure,
                    retry=args.retry,
                )
            elif args.graph_command == "resume":
                result = durable_graph.resume(
                    args.graph_id, config_path=args.config, store_path=args.store, data_dir=service.data_dir,
                )
            elif args.graph_command == "approve":
                result = durable_graph.approve(
                    args.graph_id,
                    args.node,
                    args.confirm,
                    args.actor_ref,
                    args.operation,
                    args.evidence,
                    args.usage,
                    config_path=args.config,
                    store_path=args.store,
                    data_dir=service.data_dir,
                )
            elif args.graph_command == "status":
                result = durable_graph.status(
                    args.graph_id,
                    config_path=args.config,
                    store_path=args.store,
                    data_dir=service.data_dir,
                    include_graph=args.include_graph,
                )
            elif args.graph_command == "cancel":
                result = durable_graph.cancel(
                    args.graph_id,
                    args.confirm,
                    config_path=args.config,
                    store_path=args.store,
                    data_dir=service.data_dir,
                )
            elif args.graph_command == "export":
                result = durable_graph.export(
                    args.graph_id,
                    args.output,
                    config_path=args.config,
                    store_path=args.store,
                    data_dir=service.data_dir,
                )
            elif args.graph_command == "ready":
                state = json.loads(Path(args.input).read_text(encoding="utf-8-sig"))
                graph.validate_graph_or_raise(state)
                result = {"ready": graph.ready_nodes(state), "selected": graph.parallel_ready_nodes(state, args.max_parallelism)}
            else:
                state = json.loads(Path(args.input).read_text(encoding="utf-8-sig"))
                graph.validate_graph_or_raise(state)
                if args.graph_command == "shadow":
                    predictions = json.loads(Path(args.predictions).read_text(encoding="utf-8"))
                    state = graph.record_shadow_predictions(state, predictions)
                result = graph.to_d1_summary(state)
                if not args.no_summary:
                    _write_summary(args.summary_output, result)
                    result = {**result, "control_center_summary_saved": True}
        _emit(result, json_mode=json_mode)
        return 0 if result.get("valid", True) else 1
    if command == "policy":
        import routecraft_graph_cli as durable_graph

        if args.policy_command == "status":
            result = durable_graph.policy_status(config_path=args.config, store_path=args.store)
        else:
            result = durable_graph.policy_candidates(
                config_path=args.config,
                store_path=args.store,
                data_dir=service.data_dir,
                normal_only=not args.include_special_events,
            )
        _emit(result, json_mode=json_mode)
        return 0
    if command == "agents":
        import routecraft_agents_optimizer as optimizer

        if args.agents_command == "analyze":
            result = optimizer.analyze(args.path).__dict__
        elif args.agents_command == "preview":
            result = optimizer.preview(args.path)
        else:
            result = optimizer.apply(args.path, args.confirm)
        _emit(result, json_mode=json_mode)
        return 0
    if command == "security":
        import routecraft_hardener as hardener

        if args.security_command == "analyze":
            result = hardener.analyze(args.config, args.source_root, args.baseline)
            if not args.no_summary:
                _write_summary(args.summary_output, hardener.to_d1_summary(result))
                result = {**result, "control_center_summary_saved": True}
        elif args.security_command == "preview":
            result = hardener.preview(args.config, args.source_root, args.baseline)
        else:
            result = hardener.apply(args.config, args.confirm)
        _emit(result, json_mode=json_mode)
        return 0
    if command == "benchmark":
        if args.real_preflight:
            import routecraft_real_benchmark as real_benchmark

            result = real_benchmark.benchmark_sandbox_preflight(args.codex_bin)
            _emit(result, json_mode=json_mode)
            return 0
        import routecraft_benchmark_lab as lab

        observed = json.loads(Path(args.observed).read_text(encoding="utf-8")) if args.observed else None
        result = lab.compare(lab.load_fixture(args.fixture), observed)
        if not args.no_summary:
            _write_summary(args.summary_output, lab.to_d1_summary(result))
            result = {**result, "control_center_summary_saved": True}
        _emit(result, json_mode=json_mode)
        return 0
    if command == "update":
        if not args.apply:
            raise RouteCraftLocalError("update は --apply を明示してください。既存device bootstrapを実行します。")
        import routecraft_device as device

        if not args.memory_remote:
            raise RouteCraftLocalError("update には --memory-remote が必要です。")
        update_args = argparse.Namespace(
            source_dir=args.source_dir or device.SOURCE_DIR,
            memory_dir=args.memory_dir or device.MEMORY_DIR,
            source_branch=args.source_branch or device.SOURCE_BRANCH,
            memory_branch=args.memory_branch or device.MEMORY_BRANCH,
            source_remote=args.source_remote or device.SOURCE_REMOTE,
            memory_remote=args.memory_remote,
            allow_first_device=args.allow_first_device,
            enable_project_source_guard=args.enable_project_source_guard,
            github_owner=args.github_owner,
        )
        result = device.bootstrap(update_args)
        _emit(result, json_mode=json_mode)
        return 0
    if command == "migrate":
        if args.migrate_command == "graph-config":
            import routecraft_graph_cli as durable_graph

            existing = None
            if args.existing:
                existing = json.loads(Path(args.existing).read_text(encoding="utf-8-sig"))
                if not isinstance(existing, dict):
                    raise RouteCraftLocalError("既存Graph configはJSON objectでなければなりません。")
            result = durable_graph.migrate_config(
                config_path=args.config,
                existing=existing,
                apply=args.apply,
                confirmation=args.confirm,
            )
            _emit(result, json_mode=json_mode)
            return 0
        if args.migrate_command == "endpoint":
            import routecraft_endpoint_migration as endpoint_migration

            if args.apply:
                result = endpoint_migration.apply(args.config, args.old_url, args.new_url, args.confirm or "")
            else:
                result = endpoint_migration.preview(args.config, args.old_url, args.new_url)
            _emit(result, json_mode=json_mode)
            return 0
        if args.confirm != "MIGRATE":
            raise RouteCraftLocalError("migrate には --confirm MIGRATE が必要です。")
        if args.migrate_command == "local-db":
            result = service.initialize()
        else:
            result = service.import_routecraft_store(args.project, args.input)
        _emit(result, json_mode=json_mode)
        return 0
    if command == "backup":
        result = service.backup(args.output)
        _emit(result, json_mode=json_mode, human=f"バックアップ: {result.get('output', '')}")
        return 0
    if command == "restore":
        result = service.restore(args.input, args.confirm)
        human=f"復元しました。事前バックアップ: {result.get('pre_restore_backup', '')}"
        if result.get("warnings"):
            human += f" / 警告: {', '.join(result['warnings'])}"
            if result.get("retained_rollback"): human += f" / 保持されたrollback: {result['retained_rollback']}"
        _emit(result, json_mode=json_mode, human=human)
        return 0
    if command == "export":
        result = service.export_memories(args.project, fmt=args.format, output=args.output, safe=args.safe)
        _emit(result, json_mode=json_mode, human=f"書き出しました: {result.get('output', args.output)}")
        return 0
    if command == "import":
        result = service.import_project_package(args.input, conflict=args.conflict)
        _emit(result, json_mode=json_mode)
        return 0
    if command == "ui":
        from .ui import run_ui

        if json_mode:
            raise RouteCraftLocalError("長時間起動する ui コマンドでは --json を使用できません。")
        return run_ui(service, port=args.port, open_browser=not args.no_browser)
    raise RouteCraftLocalError(f"未対応のコマンドです: {command}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    json_mode = bool(getattr(args, "global_json", False) or getattr(args, "json", False))
    try:
        service = RouteCraftService(args.data_dir)
        return _handle(args, service, json_mode)
    except RouteCraftLocalError as exc:
        payload = {"ok": False, "error": {"code": exc.__class__.__name__, "message": str(exc)}}
        if json_mode:
            print(_json_text(payload))
        else:
            print(f"routecraft: {exc}", file=sys.stderr)
        return int(getattr(exc, "exit_code", 2))
    except KeyboardInterrupt:
        if json_mode:
            print(_json_text({"ok": False, "error": {"code": "Interrupted", "message": "中断されました。"}}))
        else:
            print("routecraft: 中断されました。", file=sys.stderr)
        return 130
    except Exception as exc:  # Defensive CLI boundary; debug mode preserves traceback.
        if os.environ.get("ROUTECRAFT_DEBUG") == "1":
            raise
        message = f"予期しないエラーです。ROUTECRAFT_DEBUG=1 で詳細確認できます: {exc.__class__.__name__}"
        if json_mode:
            print(_json_text({"ok": False, "error": {"code": "InternalError", "message": message}}))
        else:
            print(f"routecraft: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
