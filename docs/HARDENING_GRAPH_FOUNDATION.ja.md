# RouteCraft 0.6 Hardening / Graph Foundation

このfoundationは、既存のRouteCraft routing loopを置き換えません。責務を次の3層に分けます。

```text
Task間    Decision Memory（検証済み判断の再利用）
Task内    Execution Graph（Unit依存・ready・merge・return path）
Unit内    produce → check → correct（bounded improvement loop）
```

## Real Agent Benchmark

`routecraft benchmark --real-preflight` はmodelを呼ばず、使い捨てのUnified Plugin登録、solver/acceptance permission profile、broker auth非読取、private home非読取、network遮断、acceptance write遮断を検証する。Windows elevated sandbox helperが未承認ならfail closedする。

現在のCodex CLI config schemaにはmodel生成量をrun単位で強制停止するhard capがない。このため`--max-tokens-per-run`は完了後accountingであり、実行前にrun数・予約量・この限界を表示し、`--confirm-token-guard POST_RUN_ACCOUNTING_ONLY`を要求する。timeout、逐次実行、総run上限は別に強制するが、hard provider capとは表示しない。

`samples/real-agent-benchmark-suite.json`は、small bug fix、multi-file bug fix、refactoring、failing-test investigation、new bounded feature、CI fix、security configuration fix、context-heavy investigation、documentation consistency、migration compatibilityの10ケースを、使い捨てGit repositoryとして生成します。Production repositoryは変更しません。

```powershell
python plugins/codex-routecraft/scripts/routecraft_real_benchmark.py list
python plugins/codex-routecraft/scripts/routecraft_real_benchmark.py run `
  --output-dir .\local-benchmark-results `
  --parallelism 3 `
  --device-id <opaque-device-id>
python plugins/codex-routecraft/scripts/routecraft_real_benchmark.py summarize `
  --results-dir .\local-benchmark-results `
  --device-id <opaque-device-id>
```

比較modeはA=RouteCraft OFF、B=ON/Decision Memory OFF、C=ON/Recall、D=ON/Full Memory、E=Graph Observe、F=Graph Enforceです。Aはplugin disableとRouteCraft marker不在を検証します。B/C/Dは現在のorchestration policy treeをSHA-256で固定した`AGENTS.md` contractをfixture baselineへ入れ、最初と最後のagent messageを厳密検証します。C/DのMemory usefulはsolverが返したrecall順位をevaluatorへ往復できた場合だけ実測値にします。Dはsynthetic fixtureをDecision Storeへ昇格させず、検証後のlearning gateを`no_reusable_learning`で閉じます。そのため永続Learnによる効果はこのsuiteの測定対象外です。

Eは実model呼出しより前にGraph IR v1をcompileし、専用SQLiteへcompile checkpointを確定した場合だけ成立します。実処理は0.6 routing pathで行うため、これはobserve treatmentです。Fはallowlistだけで成立せず、hostがtool/capabilityを強制し、artifact hashを再計算したtrusted execution receiptを返せる場合だけ実行可能です。境界がない環境ではmodelを呼ぶ前に`ENFORCE_BOUNDARY_UNAVAILABLE`で停止し、Eへ暗黙fallbackしません。既定pilot matrixはFを含めません。

raw prompt、fixture source、Codex NDJSON、受入テスト出力はcaller指定のlocal artifactにだけ保存します。Windows sandboxのACLを壊さないためrunnerはartifactを自動削除しません。D1 adapterはmetric別のsample size、available count、mean、median、range、success rate、confidenceだけを返します。取得不能値は`null`です。10未満のsampleは`INSUFFICIENT EVIDENCE`として扱います。policy/evaluator条件に失敗したrunを含むmodeは、sample数が10あっても`failed`となり、値をmeasuredとして集計しません。

旧`routecraft_benchmark_lab.py`はruntime plumbing / regression benchmarkとして残り、実Agent Benchmarkの代用にはしません。

## Security validation

`samples/security-dogfood-classifications.json` は、2026-08-25 のread-only dogfood scanで内容を確認した5件だけを `false_positive` として固定する。対象はRule Engine自身の正規表現リテラル2件と、その検出回帰fixture 3件であり、production sourceのFindingを抑制するbaselineではない。fingerprintが変わった場合は自動追随せず、再確認する。

`samples/security-validation-fixtures.json`は、登録済みruleごとにvulnerable/safeのpairを持ちます。validationはrule/category別のcoverage、TP/TN/FP/FN、detection rate、false-positive rateをpair単位で算出します。

```powershell
python plugins/codex-routecraft/scripts/routecraft_security_validation.py `
  --fixtures samples/security-validation-fixtures.json
python plugins/codex-routecraft/scripts/routecraft_security_validation.py `
  --fixtures samples/security-validation-fixtures.json `
  --dogfood-root . `
  --dogfood-root <another-repository> `
  --dogfood-classifications .\reviewed-fingerprints.json `
  --d1-summary `
  --output .\local-security-validation.json
```

Dogfoodはread-onlyです。`clean`は「有効ruleでfindingを検出しなかった」という観測であり、安全保証ではありません。検出不能なscope、未対応rule、false positive、uncertainは別集計にします。初版のcoverageは登録済みstatic-signal ruleが分母で、live HTTP response header検査はno-network scope外です。`ruleset_digest`はrule metadataだけでなくmatcher source、validator source、正規化済みfixture manifestを含みます。validation全体が実行不能ならcore metricは全て`null`、pairが0件なら分母を持たないrateだけを`null`にします。

## Legacy observation

Legacy判定はcallerが収集したfactsの検証・ledger化だけを行い、プロセス停止、startup変更、Scheduled Task変更、file削除を行いません。初版observer自身はScheduled Task/startup/collector履歴を自動収集せず、継続周期への配線も行いません。componentごとにenabled/running、replacement health、連続正常cycle、欠損snapshot、重複ingestion、最終errorをledgerへ記録します。

`superseded`に進めるのは、replacementがhealthyで、最低3回の連続正常cycleがあり、欠損と重複が0という証拠が揃った場合だけです。`archived`は人間の明示判断後に別workflowで扱い、自動設定しません。観測不足は`observing`または`unknown`とnullable metricで表します。

## Execution Graph

Graph modeは`off | observe | enforce`です。既定は`observe`で、現行routingを実行しながらGraph IR v1のplanをcompileし、専用SQLiteへcheckpointします。`enforce`はHardening Gate、task-class allowlist、trusted host execution/evidence boundaryのすべてが必要です。どれかが欠ける場合はfail closedし、`observe`へ暗黙fallbackしません。

```powershell
python plugins/codex-routecraft/scripts/routecraft.py graph validate --input graph.json --json
python plugins/codex-routecraft/scripts/routecraft.py graph plan --input graph.json --mode observe --json
python plugins/codex-routecraft/scripts/routecraft.py graph status --graph-id <graph_id> --json
python plugins/codex-routecraft/scripts/routecraft.py graph resume --graph-id <graph_id> --json
```

Graph IR v1のNode TypeはAgent、Tool、Deterministic、Gate、Merge、Human Approval、Memory Recall、Benchmark、Security、Checkpointです。Nodeはobjective、dependencies、ownership、input/output schema、verification、retry policy、lane/risk、stateを持ちます。ready計算、cycle検出、typed gate branch、bounded send-back、stable ID/hash、attempt accounting、構造互換mergeは決定的です。

orchestration skillは`ROUTECRAFT PLAN`後、`graph validate`と`graph plan`でGraph IR v1を専用SQLiteへ生成・検証し、compile checkpointを確認します。observeのplanned summaryは構造projectionであり、success/quality/token/duration比較の実測ではありません。`graph create --state-output`は旧`units` JSONの0.6 compatibility adapterとしてのみ残し、0.7 checkpoint/resume、enforce、Benchmark E/Fの証拠には使用しません。Hardening Gate Aは7つの完全なboolean checkだけを受理し、欠落・未知key・非boolean・失敗が1つでもあればenforce要求を拒否します。

短いreturn pathは失敗Unitだけを`retry_pending`へ戻します。上流outputが変わる長いreturn pathは、そのoutputへ依存するdownstreamだけをinvalidate/reopenし、独立して`ACCEPTED`になったUnitは保持します。attempt/unit、graph steps、child runs、wall time、retry budgetを超えるとconvergence failureで停止します。

verified constraintはGraph内で再利用できます。Decision Storeへexportできるのは、Graph全体がacceptedとなり、verification evidenceが揃ったconstraintだけです。自動Learnやraw output exportは行いません。

## Collector schema v4 / privacy

v4は次の4tableをadditiveに追加します。

- `benchmark_metric_evidence`
- `security_validations`
- `graph_runs`
- `legacy_components`

既存v1〜v3 payloadとtableは維持します。migrationに`DROP`、履歴削除、既存columnの意味変更はありません。各collectionのquery/write失敗は分離し、利用可能なpanelだけを継続します。

D1へ送らないもの: raw prompt、conversation、source code、finding detail、absolute/relative path、repository名、workspace、artifact、raw session/task/Decision Record ID、credential、secret。端末とrunの識別子はopaque値だけです。

`routecraft doctor`はGraph Engine、Graph Mode、Graph Schema、Benchmark Evidence、collector schemaを報告します。Control Centerへ未deployのlocal変更や未観測のLegacy replacementをPASSとして扱いません。
