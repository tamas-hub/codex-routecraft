# RouteCraft Local Runtime 0.7 Architecture

本書はGraph IR v1、state machine、SQLite persistence、scheduler、privacy projection、migrationの実装契約である。曖昧な場合は [ADR-0007](ADR-0007-EVIDENCE-DRIVEN-DURABLE-GRAPH.ja.md) と本書を優先し、0.6互換動作を破壊しない。

## Product boundary

```text
Local Runtime 0.7                               Control Center Add-on 0.3
┌──────────────────────────────────────────┐    ┌────────────────────────────┐
│ Intent Contract                          │    │ Web Dashboard              │
│ Planner -> Compiler -> Scheduler         │    │ aggregate/node graph views │
│ Graph State SQLite / Checkpoint / Resume │───>│ D1 additive schema v4      │
│ Memory Local / Decision / Outcome        │    │ trends/fleet/alerts        │
│ Benchmark / Security / Doctor / CLI      │    └────────────────────────────┘
└──────────────────────────────────────────┘

CONTROL_CENTER_ENABLED=false または通信失敗: local pathは同じ結果まで継続する。
```

登録境界は`1 Unified Plugin / 1 Unified Collector / 1 Control Center`。Codex plugin registrationを増やさない。

## Runtime layers

```text
Intent Contract
    │
    ▼
Sol Graph Planner ── machine-readable Graph IR revision N
    │
    ▼
Graph Compiler / Static Validator ── fail closed
    │
    ├─ canonical IR + input hashes
    └─ compile checkpoint
    ▼
Durable Scheduler (max parallelism default 3)
    │
    ├─ deterministic executor
    ├─ host Tool / Agent adapter
    ├─ evidence Gate / Security / Quality
    ├─ Human Approval interrupt
    └─ deterministic Merge / semantic merge handoff
    ▼
Global Acceptance Gate
    ├─ accept -> Verified Learning / Outcome
    ├─ fail -> selective retry
    └─ hidden dependency / convergence -> revision N+1
```

推奨package ownership:

```text
routecraft_graph/
  constants.py     enums/version
  canonical.py     stable JSON/hash/ID
  contracts.py     Intent/Evidence/Progress/Constraint validation
  ir.py            Graph IR parse/serialize
  compiler.py      static validation + compiled projection
  scheduler.py     ready/critical path/lock selection
  state.py         state-machine transitions
  store.py         SQLite schema/checkpoint/receipts
  engine.py        compile/run/resume/replan orchestration
  policy.py        config/lane registry/allowlist/policy candidates
  telemetry.py     local outcome + privacy-safe v4 projection
  migration.py     0.6 -> 0.7 config/store migration and rollback manifest
```

既存`routecraft_execution_graph.py`は0.6 foundation compatibility facadeとして維持できるが、canonical IRとdurable stateを一体化してはならない。

## Adaptive complexity

Graph昇格は通常コードで説明可能なdecisionを記録する。

Single Node Fast Path:

- 明確な1ファイル／低risk／短時間／依存なし。
- `Request -> Single Node -> Verification -> Accept`。
- 0.6 routing pathを維持し、observeでは1-node graphをshadow記録できる。

Graph Path:

- 複数の意味あるworkstream、dependency、parallel候補、複数gate、migration、security、CI/CD、multi-file refactor、長時間、resume価値、partial failure見込みのいずれか。
- `complexity_decision` evidenceへtriggerと理由を記録する。

## Intent Contract

必須key:

```json
{
  "request_summary": "string",
  "objectives": ["string"],
  "non_goals": ["string"],
  "constraints": ["string"],
  "acceptance_criteria": [{"criterion_id": "AC-1", "statement": "string"}],
  "risk_level": "low|medium|high|critical",
  "external_mutations": [{"kind": "string", "target_scope": "string", "reversible": true}],
  "approval_requirements": [{"operation": "string", "required": true}],
  "privacy_boundary": {"local_only": ["string"], "exportable": ["string"]},
  "budget": {"max_tokens": null, "max_duration_seconds": null, "max_child_runs": null},
  "deadline_if_known": null
}
```

欠落key、unknown risk、自由文の外部mutation、acceptance criterionゼロはcompile error。未知値は`null`であり、`0`へ変換しない。

## Graph IR v1

IRはrevisionごとにimmutableなcanonical JSONとして保存する。

```json
{
  "graph_id": "g_<stable-id>",
  "graph_schema_version": 1,
  "graph_revision": 1,
  "policy_version": "routecraft-production-v1",
  "task_class": "multi_file_refactor",
  "mode": "observe",
  "event_classification": "normal",
  "nodes": [],
  "edges": [],
  "contracts": {"intent": {}, "global_acceptance": []},
  "constraints": [],
  "budgets": {},
  "created_at": "RFC3339 UTC",
  "updated_at": "RFC3339 UTC",
  "status": "DRAFT"
}
```

Top-level exact-key validationを行い、unknown `graph_schema_version`はfail closedする。

### Node Contract

全node必須key:

```json
{
  "node_id": "n_compile",
  "node_type": "DETERMINISTIC",
  "objective": "Graph IRを検証する",
  "dependencies": [],
  "ownership": {"workstream": "graph", "write_scopes": []},
  "input_schema": {"type": "object"},
  "output_schema": {"type": "object"},
  "lane": "none",
  "reasoning_effort": "none",
  "risk": "low",
  "capability_profile": "deterministic-v1",
  "allowed_tools": [],
  "denied_operations": [],
  "verification": {"required_evidence_types": ["schema_result"]},
  "gate_policy": {"required": true, "inconclusive": "FAIL", "global": false},
  "retry_policy": {
    "max_attempts": 1,
    "max_tokens": null,
    "max_duration_seconds": 30,
    "max_failed_gates": 0
  },
  "status": "PENDING",
  "attempt": 0,
  "input_hash": null,
  "output_hash": null,
  "evidence_refs": [],
  "gate_result": null
}
```

Node type:

- `AGENT`
- `TOOL`
- `DETERMINISTIC`
- `GATE`
- `MERGE`
- `HUMAN_APPROVAL`
- `MEMORY_RECALL`
- `BENCHMARK`
- `SECURITY`
- `CHECKPOINT`
- `QUALITY`

`lane=none`はLLMを使わないnodeだけ。Agent／semantic Merge／semantic QualityはLane Registryに存在するlaneを要求する。

Node state:

`PENDING / READY / RUNNING / ACCEPTED / FROZEN / FAILED / INVALIDATED / BLOCKED / SKIPPED / CANCELLED`

terminal successは`ACCEPTED / FROZEN / SKIPPED`だが、Global GateはSKIPPEDがacceptance criterionを欠落させないことを別途検証する。

### Edge Contract

```json
{
  "from": "n_a",
  "to": "n_b",
  "edge_type": "depends_on",
  "condition": null,
  "data_contract": {"producer": "#/result", "consumer": "#/input"}
}
```

Edge type:

`depends_on / fan_out / sequence / merge / gate_pass / gate_fail / send_back / constraint_feedback`

`depends_on / fan_out / sequence / merge / gate_pass / gate_fail`から導出されるdependency graphはDAG。`send_back / constraint_feedback`はscheduler dependencyへ加えず、bounded control transition tableで管理する。

## Ledgers

Evidence record:

```json
{
  "evidence_id": "ev_<stable-id>",
  "classification": "FACT",
  "evidence_type": "test_result",
  "statement": "154 unit tests passed",
  "source_kind": "local_command",
  "artifact_hash": "sha256:...",
  "result": "PASS",
  "created_at": "RFC3339 UTC",
  "node_id": "n_test"
}
```

Classification:

`FACT / HYPOTHESIS / ASSUMPTION / VERIFIED_CONSTRAINT / RECOMMENDATION`

Hypothesis／AssumptionはGateのrequired evidenceを満たせない。Verified Constraintは最低1件の`PASS` evidence referenceを必要とする。

Progress Ledgerはtransitionをappend-only eventとして記録し、現在stateはdeterministically集約する。書込失敗時にmemory上だけ進めない。

## Static compiler

compileは次のerror codeを全件収集し、errorが1件でもあれば実行を拒否する。

- `IR_SCHEMA_INVALID`
- `NODE_ID_DUPLICATE`
- `EDGE_ENDPOINT_INVALID`
- `DEPENDENCY_MISSING`
- `DEPENDENCY_CYCLE`
- `NODE_UNREACHABLE`
- `OWNERSHIP_CONFLICT`
- `DATA_CONTRACT_MISMATCH`
- `ACCEPTANCE_CRITERIA_MISSING`
- `GATE_MISSING`
- `CAPABILITY_INVALID`
- `APPROVAL_REQUIRED`
- `PARALLEL_WRITE_CONFLICT`
- `RETRY_BUDGET_INVALID`
- `RESOURCE_BUDGET_INVALID`
- `LANE_INVALID`
- `TASK_CLASS_NOT_ALLOWLISTED`

Input/output compatibilityは保守的に判定する。consumer required propertyがproducer schemaに存在しない、またはprimitive type集合が交差しない場合はerror。判定不能をcompatibleと推測しない。

External mutation nodeは、その前方に同一operation scopeを承認する`HUMAN_APPROVAL` nodeがあり、gate-pass dependencyで結ばれていなければcompile error。

同時実行可能nodeの`write_scopes`が同一／ancestor-descendantで重なる場合、明示sequence dependencyがなければparallel write conflict。

## Scheduler

Ready条件:

1. node stateが`PENDING / INVALIDATED / READY`で、staleな`READY`は毎回再計算される。
2. 全dependencyが`ACCEPTED / FROZEN`。
3. input hashがdependency output hashとVerified Constraintから再計算済み。
4. approval、capability、resource、external mutation、write lockが満たされる。
5. graph／node budgetが残る。

選択順は決定的:

1. critical path残長の降順。
2. risk class（criticalからlow）。
3. `node_id` lexical順。

同時選択はRUNNING nodeを含めてdefault 3。ownership／repository write scopeの衝突を除外し、external mutation nodeは単一lockとして直列化する。host adapter側のresource／model availabilityでさらに絞り込める。parallelismを使い切るために依存やriskを無視しない。

`off`と`observe`では、このschedulerはNodeを開始しない。`observe`はcompile、validate、checkpoint、shadow comparisonだけを行い、実処理は0.6 routing pathが担う。`enforce`はtask-class allowlistに一致し、Graphのopaque `policy_version`がcurrent `production_policy`と完全一致し、全Nodeについてversioned trusted host execution/evidence boundaryがexecutorを解決できる場合だけtransitionを許可する。policy/allowlistはcompile時だけでなく`ready`、`start`、result acceptance、retry/failure、Verified Constraint、Human Approval、external mutation、resumeで再検査し、plan後に失効した場合は`ENFORCE_POLICY_REVOKED`で停止する。境界未設定、contract不一致、attestation不成立は`ENFORCE_BOUNDARY_UNAVAILABLE`でfail closedし、observeへ暗黙fallbackしない。

## Deterministic executor

LLMを使わない処理:

- schema／type／capability validation
- DAG、ready、critical path、downstream closure
- canonical hash／stable ID／sort／dedupe
- retry／budget／completion accounting
- status aggregation
- compatible JSON／metric／evidence ref merge
- test result parsing
- explicit formulaによるmetric／confidence／ranking
- privacy projection

Agent／Tool nodeはhost adapterへstructured packetを出す。`ExecutionBoundary v1`のbindingはnode type、capability profile、allowed tools、denied operations、write scope、risk limitへ完全に結び付け、attempt claimとresult attestationを同じhost境界で検証する。adapterはartifactからhashを再計算し、input/output schemaとbudgetを強制し、raw transcriptをGraph StoreのEvidence Ledgerへ自動保存しない。Local Runtime sourceにはinterfaceとfail-closed kernelを実装するが、Codex hostへ接続するproduction adapterは現時点では存在しない。このためdefault allowlistは空で、standalone CLIのenforce plan／raw resultは拒否する。

## Gate and evidence

Gate result:

`PASS / FAIL / INCONCLUSIVE`

Gate acceptance条件:

- required evidence typeが全て存在する。
- evidence classificationが`FACT`または`VERIFIED_CONSTRAINT`。
- artifact hash／resultがvalid。
- Gate resultが`PASS`。

`INCONCLUSIVE`はnodeをacceptしない。high／critical riskのfresh Sol requirement、external mutationのHuman Approval requirementはcode側のgate policyで強制する。

## Merge

Deterministic Merge:

- structured metrics、status、evidence refs、non-conflicting artifact refs、compatible JSON、test summaries。
- stale input hash、ownership conflict、duplicate artifact hashを先に検査する。

Semantic Merge:

- code diff、interface、meaning、architecture、competing recommendationのconflictだけをAgent nodeへ送る。
- semantic resultもoutput schemaとGateを通す。

## Selective retry and return paths

Gate FAIL:

1. failed nodeを`FAILED`。
2. failed outputに依存するtransitive downstreamだけを`INVALIDATED`。
3. independent `ACCEPTED`を`FROZEN`。
4. failed nodeをattempt／budget内なら`READY`へsend-back。
5. output hashが変わった場合だけdownstream input hashを再計算。

上限超過は`NODE_CONVERGENCE_FAILED`。hidden dependency、interface change、security／migration risk、budget超過はgraph revision `N+1`を作り、旧revision、reason、changed nodes／edgesを保持する。

Verified Constraint:

```json
{
  "constraint_id": "vc_<stable-id>",
  "scope": "graph|node|artifact|interface",
  "statement": "string",
  "evidence_refs": ["ev_..."],
  "confidence": "high|medium|low",
  "applies_to": ["n_x"],
  "invalidates": ["n_y"],
  "created_by": "n_gate"
}
```

remaining graphへ適用する際はconstraint hashをinput hashへ含める。

## SQLite Graph State Store

Default path:

`$CODEX_HOME/routecraft/graph/state.sqlite3`（`CODEX_HOME`未設定時は`%USERPROFILE%/.codex/...`）

Memory Local、Decision Store、plugin cacheと別directory。configで別pathを指定できるが、Memory Local DBまたはDecision Store配下を指す場合は拒否する。

Store schema v1:

- `store_metadata`
- `graphs`
- `graph_revisions`
- `node_states`
- `edge_states`
- `ledger_entries`
- `constraints`
- `checkpoints`
- `idempotency_receipts`
- `graph_events`
- `outcomes`
- `policy_candidates`

SQLite settings:

- `PRAGMA foreign_keys=ON`
- `PRAGMA journal_mode=WAL`
- `PRAGMA synchronous=FULL`
- bounded `busy_timeout`
- mutation transactionは`BEGIN IMMEDIATE`

未知`PRAGMA user_version`、`quick_check != ok`、checkpoint chain不一致はwrite／resumeをfail closedする。各revisionのcheckpoint headは`store_metadata`内の別anchorへ同一transactionで記録し、最新tailだけの欠落も検出する。migration前にSQLite backup APIでconsistent backupを作る。

Checkpoint境界:

- compile完了
- Node acceptance
- Merge完了
- external mutation完了
- Global Gate完了
- Human Approval前後
- Replan前後

Checkpoint payloadはIR revision、node／edge state、Gate verdict、budget、constraint、evidence refs、receipt refsをcanonicalizeし、`previous_hash + payload_hash + sequence`をhashする。state snapshotと重要checkpointは同一SQLite transactionで確定する。send-back前の`gate_resolution`と適用後の`send_back`を別checkpointにし、前者だけで停止した場合はresumeがpending send-backを決定論的に完遂し、後者の確定後は旧Gate branchへ戻らない。

`cancel`はcancelled state snapshotと`cancelled` checkpointを、Verified Constraintは（observe時に新規となるEvidence Ledger、constraint row、state snapshot、`constraint_applied` checkpoint）を一つの`BEGIN IMMEDIATE` transactionで確定する。後者はenforce時に既存のattested Evidence Ledgerだけを参照し、新規Evidenceを混在させない。呼出元がcommit直後に停止して同じconstraintを再送しても、同一内容はidempotentに現在のstateを返し、異なる内容で同じconstraint IDを再利用する要求は拒否する。いずれのmaterial transitionも`updated_at`を現在UTCへ進めるため、Collector/D1はcheckpoint sequenceを推測せず更新を観測できる。

このhash chainとDB内head anchorのthreat boundaryは、破損、partial write、単独tail truncationの検出である。鍵付き署名ではなく、同じOS user権限でDB、chain、anchorを整合するよう同時改変する攻撃者に対するauthenticityは提供しない。

## Idempotency state machine

`PREPARED -> COMMITTED | FAILED | UNKNOWN`

- key = SHA-256(canonical graph_id、node_id、attempt、input_hash、operation scope)。
- `COMMITTED`は同じkeyの再実行をskipし、保存済みresult referenceを返す。
- `PREPARED`のままresumeした場合、remote照会またはprovider idempotencyで結果を確定できればreconcileする。
- 確定不能は`UNKNOWN`としてblockし、自動再実行しない。

## Lane Registry v1

Graphはmodel名でなくlaneを参照する。

```json
{
  "registry_version": 1,
  "lanes": {
    "luna": {"capability_class": "bounded", "cost_class": "low", "context_class": "small", "reasoning_levels": ["low", "medium", "max"], "allowed_task_types": ["bounded_implementation"], "risk_limit": "medium", "provider_mapping": "local-profile:luna"},
    "terra": {"capability_class": "judgment", "cost_class": "medium", "context_class": "large", "reasoning_levels": ["medium", "high"], "allowed_task_types": ["integration", "reviewed_implementation"], "risk_limit": "high", "provider_mapping": "local-profile:terra"},
    "sol": {"capability_class": "architecture", "cost_class": "high", "context_class": "largest", "reasoning_levels": ["high", "ultra"], "allowed_task_types": ["architecture", "acceptance", "fresh_review"], "risk_limit": "critical", "provider_mapping": "host:parent-sol"}
  }
}
```

provider mappingはlocal configで差し替えられる。Graph Policyをmodel renameで書き換えない。

## Config v1

```json
{
  "config_version": 1,
  "graph": {
    "mode": "observe",
    "max_parallelism": 3,
    "max_node_attempts": 3,
    "max_graph_revisions": 3,
    "state_store": null,
    "checkpoint": true
  },
  "policy": {
    "production_policy": "routecraft-production-v1",
    "allowlisted_task_classes": []
  },
  "control_center": {"enabled": false}
}
```

0.6にconfigがなければこのdefaultを新規作成する。既存Control Center opt-in値は明示migration時だけ引き継ぐ。unknown config versionはfail closedし、0.6 fallbackを破壊しない。

## Memory and Policy Lab

Graph acceptance後:

`Verified Constraint -> applicability review -> Decision Store Candidate`

raw graph、prompt、source、raw node output、自由なreflectionは保存しない。Outcome Memoryはtask class、graph template、lane、success、retry、duration、token、security、memory usefulnessをlocal aggregateとして持つ。

Policy status:

`DRAFT / SHADOW / CANDIDATE / APPROVED / REJECTED / RETIRED`

Production Policy更新はHuman Approval Nodeとfresh evidenceを要求する。

## Special event segmentation

Allowed classification:

`normal / benchmark_run / migration_event / incident_response / token_burn_event / reset_expectation / manual_stress_test / release_validation`

Policy Lab／benchmark trendは明示指定がなければ`normal`だけを学習対象にする。special eventをnormalへ暗黙変換しない。

## Telemetry v4 privacy contract

Collectorはstrict allowlist projectionだけを送る。relation用IDはrandom／opaque、nodeはrun内ordinalであり、objectiveやpathを含まない。

送信可能:

- graph mode、revision count、node／edge count、parallel width、critical path length
- node ordinal、node type、lane、status、attempt count、gate result
- dependency ordinal、bounded event type
- duration、nullable token summary、retry／send-back／accepted／failed／invalidated／constraint／checkpoint count
- policy candidateのopaque ID、enum status/change kind、sample size、confidence、nullable expected benefit／known-risk enum
- security ruleのstable rule IDとTP／TN／FP／FN／coverage／rates

送信禁止:

- prompt、conversation、source code、file content、absolute／relative path
- repository／workspace名、secret、credential
- raw worker packet、raw node output、finding detail
- Memory本文、Decision本文、Verified Constraint statement

projection failureまたはtransport failureはlocal graph stateを変更しない。

Durable CLI/host adapterは、`graph plan/run/resume/approve/cancel`のdurable mutation後と明示的`graph export`時に、`CODEX_HOME/routecraft/graph/latest-collector-v4.json`へ`graph_runs`、`graph_node_metrics`、`graph_events`だけから成る単一のatomic bundleをbest-effortでmaterializeする。Unified Collectorはこのbundleを優先し、3 familyを別々の「latest」fileとして組み合わせない。Collectorがこのlocal cacheを読めない／検証できない場合もGraph executionとuser指定のlocal exportは成功のままにする。将来のtrusted production host adapterも、durable transition後に同じhost-side refresh contractを呼ぶ。Graph kernel自体はCollectorをimportしない。

Graph bundleは合計75 rowまでである。超過時はnode/eventを分割送信せず、正確な`graph_runs` aggregateだけへdeterministicにdowngradeする。75 rowを超えるraw bundleはCollector validationでもrejectする。Collectorはdevice IDをcollection cycleへ正規化する際、graph run IDとnode/eventのforeign keyを同一bundle内で一緒に更新し、mixed/stale bundleは全familyをomitする。

`gate`と`send_back`のevent rowは、hash-chainとhead anchorを検証済みのcheckpoint履歴から導出する。GateのFAIL→retry→PASSをlatest node statusで上書きせず、履歴countへ残す。Dependencyとnode-transition rowはlatest IRの構造投影であり、全tool/agent transitionの完全なcausal traceではない。この区別をControl Centerにも表示する。

`accepted_count`は厳密に`ACCEPTED` nodeだけを数え、非選択typed branchの`SKIPPED`を水増ししない。run-level `send_back_count`は認証済みcheckpointの実測だが、nodeへの帰属を証明できない場合のnode-level値は`null`とする。`send_back`と`constraint_feedback`はcontrol transitionであり、dependency eventへ投影しない。最終Global GateがPASSしたGraphでは、履歴上の非global Gate FAIL/INCONCLUSIVEをrun failureへ読み替えない。

## D1 additive schema v4 contract

既存v1〜v3 table／rowを維持し、同一additive migrationで次を追加する。

- `benchmark_metric_evidence`
- `security_validations`
- `legacy_components`
- `graph_runs`
- `graph_node_metrics`
- `graph_events`
- `policy_candidates`
- `security_rule_metrics`

`graph_runs`はrun aggregate、`graph_node_metrics`はprivacy-safe node ordinal、`graph_events`はtimeline／dependency／gate transition、`policy_candidates`はstructured public projection、`security_rule_metrics`はconfusion matrixを持つ。既存tableのDROP／DELETE／destructive ALTER、history rewriteは禁止。

Rollbackはapplicationをv3 readerへ戻し、新tableを残置する。schema自体をdown migrationで削除しない。

## Control Center 0.3 contract

既存8 viewを維持し、独立dashboardを増やさない。

Execution subview:

`Runs / Routing / Graph / Gates / Failures`

Graphはtimeline、critical path、status、dependency ordinal、lane、gate、retry、send-back、accepted/frozen、invalidated、constraint count、nullable token／durationを表示する。semantic objectiveやpathは表示しない。

BenchmarkはCurrent vs Candidate、Real／Estimated、sample size、confidence、Policy Candidate。Securityはfindingとrule coverage／TP／TN／FP／FNを分け、常に次を表示する。

`No findings detected by enabled rules. This does not guarantee repository safety.`

Special Event filterは`All / Normal only / Special events`。HealthはGraph Engine、State Store、Mode、Schema、Checkpoint、Collector、Legacy、Benchmark evidenceを表示する。

## Migration sequence and rollback

1. Inventory／private backup。
2. Runtime 0.7 sourceを専用worktreeでbuild／test。
3. Graph config/storeを新規作成。Memory／Decisionは移動しない。
4. observe shadowと0.6 routing fallbackを検証。
5. additive D1 v4 dry-run、Control Center local test/build/UI。
6. Runtime install／doctor／observe成功後だけ単一plugin cacheを更新。
7. D1 migration後にcompatible API、次にSites source/versionを反映。
8. task-class allowlistはpilot evidence後だけ更新。

Rollback:

- Runtime: backed-up source／plugin cache／configへ戻し、Graph mode off。Graph DBは削除せずread-only archive。
- Control Center: prior saved Sites version/source commitへ戻す。
- D1: v4 tableを残置し、v3 readerへ戻す。row削除／table dropなし。
- Memory／Decision: migration対象外。既存backupからのrestoreは別承認操作。

## Final readiness rule

- `READY-IN-OBSERVE`: engine、checkpoint/resume、privacy、regressionがPASS。enforce evidenceは不足可能。
- `READY-SELECTIVE-ENFORCE`: allowlisted task classでpilot evidenceもPASS。
- `NOT READY`: regression、privacy、durable state、migration、selective retryのいずれかがFAIL／INCONCLUSIVE。
