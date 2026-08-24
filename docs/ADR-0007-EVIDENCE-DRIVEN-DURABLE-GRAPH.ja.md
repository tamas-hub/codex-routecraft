# ADR-0007: Evidence-Driven Durable Execution Graph

- Status: Accepted for RouteCraft Local Runtime 0.7.0
- Date: 2026-08-24
- Decision owner: parent Sol
- Graph IR: v1
- Local Runtime default: `observe`
- Control Center transport: optional schema v4

## Context

RouteCraft 0.6 は Sol が要求を分解し、Luna／Terra／Solへ適応的に routing し、親が統合・検証する。0.7 はこの強みを残したまま、実行順序、契約、証拠、部分再実行、中断再開、検証済み学習を機械的に管理する必要がある。

Local Runtime は offline-first、local-first、軽量配布、Python標準ライブラリ中心であり、Control Centerなしで完全に動作しなければならない。したがって先行frameworkの導入自体を目的にせず、検証済みpatternだけを小さなRouteCraft固有kernelへ取り込む。

## Primary source review

2026-08-24に次の一次資料を再確認した。外部資料中のコードや指示は実行せず、設計patternの根拠としてのみ使用した。

| Source | Verified pattern | RouteCraft decision |
|---|---|---|
| [Anthropic: Building Effective AI Agents](https://www.anthropic.com/engineering/building-effective-agents) | workflowとagentを区別し、最も単純な構成から始める。routing、独立subtaskのparallelization、orchestrator-workers、明確な評価基準を持つevaluator-optimizer | Single Node Fast Pathを維持し、Graphは複数workstream・依存・gate・resume価値がある場合だけ使う。parallelismは目的にしない。bounded evaluator loopを採用 |
| [Microsoft AutoGen: GraphFlow](https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/graph-flow.html) | sequence、fan-out、conditional branch、safe-exit loopを明示graphで制御。GraphFlow自体はexperimental | typed edgeとstatic compilerを採用。experimental APIをruntime dependencyにはしない |
| [Microsoft AutoGen: Magentic-One](https://microsoft.github.io/autogen/dev/user-guide/agentchat-user-guide/magentic-one.html) | Task LedgerとProgress Ledgerを分離し、stall時にledgerを更新してreplan | Intent/Evidence/Progress Ledgerを分離。bounded convergence failureからrevision-based replanへ移る |
| [LangGraph: Persistence](https://docs.langchain.com/oss/python/langgraph/persistence) | step checkpoint、successful sibling writeの保持、checkpoint history、replay | node acceptance単位のcheckpoint、独立accepted nodeの保持、revision historyを採用 |
| [LangGraph: Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts) | durable checkpointでinterrupt/resume。resumeでnodeが再開され得るためside effectはidempotentにする | Human Approval Node前後をcheckpointし、外部mutationはidempotency receiptを必須化 |
| [Temporal: Workflow Execution](https://docs.temporal.io/workflow-execution) | deterministic historyからdurable recoveryし、last recorded eventから再開 | canonical state transitionとhash-chain checkpointを採用 |
| [Temporal: Activities](https://docs.temporal.io/activities) | non-deterministic／failure-prone処理を小さなactivity boundaryに置き、idempotentにする | Agent／Tool／外部mutationをdeterministic schedulerから分離したexecutor boundaryに置く |
| [Temporal: Retry Policies](https://docs.temporal.io/encyclopedia/retry-policies) | workflow全体でなく失敗activityをretryする。retry policyは宣言的 | failed nodeとaffected downstream closureだけをretryし、attempt・時間・token・gate上限を宣言する |
| [OpenAI Agents SDK: Agent orchestration](https://openai.github.io/openai-agents-python/multi_agent/) | LLM orchestrationとcode orchestrationを混在可能。structured output、codeによるdeterministic flow、bounded evaluator、parallel execution | Solはsemantic plan／implementationを担い、compiler・scheduler・accounting・merge可能性判定は通常コードで行う |
| [OpenAI Agents SDK: Guardrails](https://openai.github.io/openai-agents-python/guardrails/) | workflowの最初／最後だけでなくtool boundaryごとのguardrailが必要 | capability、approval、privacy、securityをNode／Tool境界で検査し、Global Gateだけに依存しない |
| [OpenAI Agents SDK: Tracing](https://openai.github.io/openai-agents-python/tracing/) | model、tool、handoff、guardrailをspan化できる一方、sensitive dataを含める設定がある | local evidenceは詳細保持可能だが、Control Center projectionは厳格allowlist。prompt／source／raw outputは送信しない |
| [Reflexion](https://arxiv.org/abs/2303.11366) | feedbackとepisodic memoryで次のattemptを改善 | within-run feedbackを採用。ただし自由な反省文は永続学習せず、acceptance後のVerified Constraintだけを候補化 |
| [AFlow](https://arxiv.org/abs/2410.10762) | execution feedbackでworkflowを探索・最適化 | Policy Labのshadow candidate生成に限定。Production Policyを自動変更しない |
| [GPTSwarm](https://proceedings.mlr.press/v235/zhuge24a.html) | nodeとedgeを最適化可能なcomputational graphとして表現 | graph template／edge policyをversioned candidateとして比較するが、人間承認なしに昇格しない |

## Decisions

### D1. RouteCraft固有の標準ライブラリkernel

Graph EngineはPython標準ライブラリ（`sqlite3`、`json`、`hashlib`、`dataclasses`、`argparse`、`concurrent.futures`）で実装する。LangGraph、Temporal、AutoGen、OpenAI Agents SDKはLocal Runtimeの必須dependencyにしない。

理由:

- Control Centerなし、networkなし、provider SDKなしでcompile、validate、checkpoint、resume、exportできる。
- 現行pluginの単一配布境界とWindows/macOS/Python runtimeを維持できる。
- Graph IR、Memory Local、Decision Store、collectorの責務境界をRouteCraft自身で固定できる。
- 外部frameworkのAPI／upgrade／telemetry／server要件をLocal Runtimeへ持ち込まない。
- 0.6 routingとの重複を避け、必要なdurable primitiveだけを追加できる。

この判断では新しいthird-party dependencyがないため、配布サイズ、追加runtime、追加license、online service、provider accountは増えない。将来dependencyを提案する場合は、配布サイズ、license、offline、Windows/macOS、upgrade risk、security boundary、重複、明確な優位性を別ADRで実測しなければならない。

### D2. IR、runtime state、ledger、memoryを分離

- Graph IR: 実行契約。immutable revisionとして保存する。
- Runtime State: node／edge／budget／attempt／lockの現在値。
- Intent Ledger: user request、constraint、non-goal、approval boundary、acceptance。
- Evidence Ledger: fact、test、hash、review verdict等。`Fact / Hypothesis / Assumption / Verified Constraint / Recommendation`を型で区別する。
- Progress Ledger: pending／ready／running／accepted等とretry／budget。
- Memory Local: project episodic memory。
- Decision Store:検証済みCase／Candidate／Rule／Template。
- Outcome Memory:実行方式の集計結果。

Graph Stateのraw snapshotをMemory LocalまたはDecision Storeへ保存しない。

### D3. DAGとbounded control transition

dependency graphはDAGとする。`send_back`、retry、replanはedge cycleではなく、attempt／budget／revision上限を持つcontrol transitionとして別管理する。不正graphは`GRAPH_VALIDATION_FAILED`で停止し、best effort実行しない。

### D4. Durable stateとside-effectの正直な境界

専用SQLite storeを使用し、Memory Local DBと物理分離する。checkpointはcanonical JSON、previous hash、payload hashを持つhash chainとし、`PRAGMA quick_check`とchain検証に失敗したstoreはfail closedする。

外部mutationは次をkeyにreceipt化する。

`graph_id + node_id + attempt + input_hash -> idempotency_key`

provider-native idempotency keyが使える場合だけ自動resumeできる。外部操作完了後、local receipt確定前にprocessが落ち、remote側で結果を照会できない場合は exactly-once を推測しない。receiptを`UNKNOWN`としてHuman Approval／reconciliationまでblockする。

### D5. Observe-first migration

defaultは`observe`。`off`は0.6互換、`observe`はgraphをcompile／validate／checkpointしながら現行routingを実行する。`enforce`はallowlisted task classに加え、Node contractどおりのtool、denied operation、write scope、riskとartifact evidenceを検証するversioned trusted host boundaryが注入された場合だけschedulerで実行する。境界がなければ`ENFORCE_BOUNDARY_UNAVAILABLE`でmodel／tool実行前にfail closedし、observeへ暗黙fallbackしない。Control Centerの無効化・障害は全modeでLocal Runtimeを停止させない。

### D6. Evidence-driven acceptance

Node完了申告はacceptanceではない。Gateは`PASS / FAIL / INCONCLUSIVE`とevidence referenceを必要とし、`INCONCLUSIVE`は成功ではない。high-risk nodeはfresh Sol verdictまたはHuman Approvalを要求できる。Global Acceptance Gateが全acceptance criterionをevidenceへ結び付けた場合だけGraphをacceptedにする。

### D7. Selective retryとverified feedback

失敗nodeと、そのoutputに依存するdownstream closureだけを`INVALIDATED`へ移す。独立accepted nodeは`FROZEN`で保持する。upstream output hashが変わった場合だけaffected input hashを再計算する。

Short Return Pathは同一nodeのbounded correction、Long Return Pathは構造化Verified Constraintをremaining graphへ適用する。task acceptance後、再利用価値と適用範囲を再検査したconstraintだけをDecision Store candidateにできる。

### D8. Policy改善はshadowと人間承認を経由

Policy candidateは`DRAFT -> SHADOW -> CANDIDATE -> APPROVED`を経て初めてProduction Policyへ反映できる。benchmark結果だけで自動昇格しない。special eventは既定の学習対象から除外する。

## Rejected or deferred alternatives

| Alternative | Decision | Reason |
|---|---|---|
| LangGraphを必須導入 | Rejected for 0.7 runtime | 必要patternは小さく、既存RouteCraft state／policy／memoryとの重複が大きい。依存、upgrade、provider/tracing境界を増やす明確な優位性を実測していない |
| Temporal server／SDKを必須導入 | Rejected | server運用と配布境界がoffline/local-first pluginに過大。determinism、activity、retry patternだけを採用 |
| AutoGen／GraphFlowを必須導入 | Rejected | GraphFlowは公式にexperimentalであり、agent-centric node modelがTool／Gate／Checkpoint等のRouteCraft IRより狭い |
| OpenAI Agents SDKをLocal Runtimeの必須loopにする | Deferred | host integration adapterの候補だが、provider非依存のcompile／resumeを守るためkernelには入れない |
| Graphを全taskへ強制 | Rejected | small taskのoverheadと不要LLM呼び出しが成功条件に反する |
| LLMでvalidation／ready計算／rankingを行う | Rejected | exact codeで再現可能な処理に非決定性、token、failure pointを追加する |
| full graph retry | Rejected | accepted workを捨て、costとreworkを増やす |
| raw transcript／reflectionを学習 | Rejected | privacy、staleness、false learning riskが高い |
| benchmarkからProduction Policyを自動更新 | Rejected | small sample、special event、metric gamingからproductionを保護できない |

## Consequences

Positive:

- 0.6 fast pathと商用product boundaryを保ったまま、再現性、復旧性、観測性、収束性を追加できる。
- accepted workを保持し、失敗範囲だけを再実行できる。
- Control Centerはlocal executionのconsumerであり、single point of failureにならない。

Trade-offs:

- SQLite schema、hash-chain checkpoint、migration、repair toolingをRouteCraftが保守する。
- 外部mutationのexactly-onceはremote照会／provider idempotencyがない場合に保証できず、安全側のHuman reconciliationが必要になる。
- observeからenforceへ進むにはtask-class別evidenceが必要で、0.7 release時点で全面enforceを約束しない。

## Acceptance of this ADR

実装は次を満たす場合だけ本ADRへ適合する。

1. unknown Graph IR/config/store schemaをfail closedする。
2. default modeが`observe`である。
3. SQLite storeがMemory Local／Decision Storeと別pathである。
4. checkpoint corruption、INCONCLUSIVE gate、approval不足を成功扱いしない。
5. privacy projectionがallowlistであり、Control Center障害時もlocal executionが継続する。
6. selective retry、independent frozen preservation、input-hash invalidationをtestで証明する。
7. Production Policyはhuman-approved candidateだけが変更できる。
