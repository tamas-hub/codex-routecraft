# RouteCraft Core / Praxis Memory / Praxis Dashboard アーキテクチャ

## 目的と適用範囲

RouteCraft v0.7.x は、既存の一体的な起動方法と運用を維持したまま、責務を次の3コンポーネントへ分ける。

- **RouteCraft Core**: 必要な場合だけ、タスク分析、Routing Hint、Host Capability判定、実行制御を行う。
- **Praxis Memory**: 使用するAIに依存せず、事実、案件、判断、失敗、解決、経験、プロジェクト状態、イベントをローカルへ保存・検索する。
- **Praxis Dashboard**: RouteCraft専用ではない共通Event Schemaから、実行、Routing、Memory、Experience、Usage、Eventを観測する。

`Praxis` は既定の表示名であり、API・DB・EventはコンポーネントIDとschema versionを正本とする。将来の名称変更で永続形式を書き換えない。

この変更は、既存のRouteCraftを廃止する全面再実装ではない。既存CLI、Graph IR v1、Memory Local、Markdown Decision Store、Collector v1〜v4、Observatory、Control Center接続を凍結面として残す段階的な分離である。

## 設計原則

1. 現行v0.7.xの挙動と入口を先に守る。
2. 3コンポーネントはversioned protocolを介し、互いの内部テーブルやprivate関数へ依存しない。
3. CoreはMemoryとDashboardがなくても動く。
4. MemoryとDashboardはCoreをimportしない。
5. 未確認のHost・Agent・Model capabilityは`null`として保持し、事実へ変換しない。
6. Routing ModeとGraph Modeを混同しない。
7. DB migrationはdry-runが既定で、移行元を変更せず、apply前にbackupを作る。
8. 特殊イベントは保存するが、通常のRecall・評価・推奨には既定で混ぜない。
9. prompt、会話、source/file本文、raw output、credential、秘密情報、端末固有pathを共通イベントへ保存しない。
10. ローカルファーストを保ち、必須のクラウド、broker、外部frameworkを追加しない。

## Before / After

### Before

```text
RouteCraft Plugin / Local Runtime
├─ Orchestration Skill + Agent profiles
├─ Graph Runtime
├─ RouteCraft Memory Local
├─ Markdown Decision Store
├─ Local Web UI
├─ Unified Collector
└─ optional Control Center adapter
```

### After

```text
                         Common Event Schema v1
                                  ▲
                                  │
┌──────────────────┐      ┌───────┴──────────┐      ┌────────────────────┐
│ RouteCraft Core  │ ───► │ local Event/API │ ◄─── │  Praxis Memory     │
│ API v1           │      │ boundary        │      │  schema/API v1     │
│                  │      └───────┬──────────┘      │  separate SQLite   │
│ routing modes    │              │                 └────────────────────┘
│ capability hints │              ▼
└────────┬─────────┘      ┌────────────────────┐
         │                │ Praxis Dashboard   │
         ▼                │ Query API v1       │
 Agent / Host adapter     │ source-neutral UI  │
                         └────────────────────┘

Compatibility layer:
routecraft CLI / Graph v0.7 / Memory Local / Decision Store / Collector v1-v4
```

## コンポーネント境界

### RouteCraft Core

`routecraft_core` はPython標準ライブラリだけで利用できる。外部Model SDKを直接importせず、実際のspawn・model呼出しは`HostAdapter`が所有する。

公開境界:

- Core API version `1`
- `RoutingRequest`
- `RoutingDecision`
- `ExecutionResult`
- `HostCapabilityRegistry`
- `MemoryPort`
- `EventSink`
- `HostAdapter`

CoreにはNull MemoryとNull Event Sinkがあり、Praxis Memoryが未導入・停止・破損していても、MemoryなしのRoutingと実行を継続できる。Memory/Event adapterのbest-effort失敗をAgent実行の成功・失敗へ読み替えない。

### Routing Mode

| mode | 意味 | 強制実行 |
| --- | --- | --- |
| `native` | Host側へ判断を委譲する。`native_routing=true`が確認できない場合はadvisoryへ明示fallbackする。 | Coreは選択しない |
| `advisory` | complexity、推奨lane、reasoning等のHintだけを返す。 | なし |
| `routecraft` | provider model名ではなくRouteCraft laneを選ぶ。Coreはdispatch意図を返し、実行可否と実dispatchはHostAdapter境界が所有する。 | Host側境界だけ |
| `legacy` | v0.7.x以前のSkill、Agent profile、CLI、Graph fallbackを維持する。既定。 | 既存経路 |

Routing ModeはGraph Modeの`off | observe | enforce`と別namespaceである。Routing Modeを変えてもGraph policy、allowlist、trusted execution boundaryは変わらない。

### Host Capability Registry

Registryは`schema_version: "1"`の宣言データであり、能力を自動推測しない。provider、host、modelをデータとして扱い、各capabilityは`true | false | null`で表す。

capabilityは、明示的に選択された`provider -> host -> model`の直系scopeだけを継承する。host未指定時にmodel値を参照せず、model未指定時に兄弟modelから推測しない。`available=false`は選択経路の親scopeを含めてdispatchを拒否し、`null`は未確認のまま保持する。

```json
{
  "schema_version": "1",
  "providers": [
    {
      "provider": "example_provider",
      "capabilities": {"available": null, "native_routing": null},
      "hosts": [
        {
          "host": "example_host",
          "capabilities": {"native_routing": null, "tool_dispatch": null},
          "models": [
            {
              "model": "example_model",
              "capabilities": {"available": null, "structured_output": null}
            }
          ]
        }
      ]
    }
  ]
}
```

将来モデル名や未公開仕様は登録しない。`configured`、`requested`、`verified`、`unverified`を同義として表示しない。

### Praxis Memory

`praxis_memory` はCoreをimportしない独立コンポーネントで、専用の`praxis-memory.sqlite3`を使用する。既存の次のストアとは物理的に分離する。

- `routecraft-local.sqlite3`
- Markdown Decision Store
- Graph State Store
- Memory評価用JSONL

データ分類:

- fact
- case
- decision
- failure
- solution
- project_state
- session
- event
- policy
- skill_metadata
- experience

Failureは`trigger / action / result / root_cause / mitigation / avoid_next_time`を第一級データとして扱う。Experienceは`task / context / strategy / execution / result / evaluation`を保持できる。Skill候補はmetadataとして保持できるが、自動昇格しない。

Recallは、取得できた項目だけを使って次のscore componentを返す。

- relevance
- recency
- confidence
- success rate
- reuse count
- project similarity
- environment similarity
- reliability

未取得値を0へ置換しない。通常Recallは`event_classification=normal`だけを対象にする。

### Praxis Dashboard

`praxis_dashboard` は`EventSource`からEvent Schema v1を読み、純粋なprojectionを作る。Core、特定provider、特定model、Control Center D1を必須依存にしない。

Query API v1:

- snapshot
- events（最大件数とcursor付き）
- sources

表示区分:

- Runtime
- Routing
- Memory
- Experience
- Usage
- Events

sourceが未設定の場合は`unavailable`と空配列を返す。未知値を0へ変換しない。破損SQLite、未知schema、想定外tableを含むmalformed sourceは`source_error`へfail closedし、利用可能な空DBとは表示しない。他sourceや既存Memory UIは停止しない。

専用`praxis-dashboard` launcherはlegacy local serviceを初期化せず、既に存在する`praxis-events.jsonl`または`praxis-memory.sqlite3`を読み取り専用で開く。SQLiteはURIの`mode=ro`、`query_only`、quick check、schema/table検査を通過した場合だけ利用する。sourceがない場合もdirectory、DB、lock、backupを作らない。専用serverは`127.0.0.1`固定で、Praxis GET API以外の書込みmethodを拒否する。

既存`routecraft ui`は初期`#dashboard`、Memory CRUD、backup/restore、CSRF、Host/Origin検査を維持する。Praxisは追加の`#praxis` viewとread-only APIであり、統合UIでもPraxis GETは同じ`mode=ro` EventSourceを使ってDDLやschema更新を行わない。既存`/api/dashboard`のkeyは削除しない。

## Common Event Schema v1

共通Eventは次のexact keyを持つ。

```json
{
  "schema_version": "1",
  "event": "task.completed",
  "event_id": "evt_example_01",
  "timestamp": "2026-08-27T00:00:00Z",
  "source": "routecraft-core",
  "provider": "openai",
  "agent": "codex",
  "model": null,
  "project": "example-project",
  "task_id": "task-example-01",
  "status": "success",
  "event_classification": "normal",
  "metadata": {}
}
```

event family:

- `task.*`
- `routing.*`
- `memory.*`
- `execution.*`
- `evaluation.*`
- `usage.*`
- `system.*`

特殊分類:

- `token_burn_event`
- `reset_expectation`
- `benchmark_event`
- `migration_event`
- `stress_test`
- `manual_override`

特殊Eventは削除せず保存する。通常平均、Recall、Policy候補、推奨値へ含めるには明示的なopt-inが必要である。

`event_id`は不変identityである。同一ID・同一payloadは冪等、同一ID・異なるpayloadはconflictとして拒否する。timestampが同一のTask/Execution Eventにcurrent stateの順序が必要な場合、sourceはtask内の非負整数`metadata.sequence`を付ける。sequenceが欠けた同時刻の異なる状態は、Dashboardがopaqueな`event_id`から順序を推測せず`unknown`として保持する。Coreの`not_dispatched`は終端`unknown`、`host_adapter_unavailable`は終端`failed`へ投影し、`running`のまま残さない。

## RouteCraft Telemetry Schema v1

Common Event v1の13個のtop-level keyは変更しない。RouteCraft固有の観測値は、任意の`metadata.routecraft_telemetry` envelopeへ加法的に格納する。envelopeがない旧EventとCollector schema v2〜v4は、Dashboard側の読み取り専用adapterで扱う。

主なfield group:

- identity: `run_id`、`session_id`
- request / decision / actual: requested、selected、route decision、actualのmodelとreasoning、`decision_source`、`decision_reason`、`decision_confidence`、`route_changed`
- memory: `memory_recall_used`、`memory_case_ids`、`rules_applied`
- usage: input / cached input / output / reasoning / total tokens、`execution_time_ms`、`retry_count`、`model_calls`、`tool_calls`、`file_reads`
- versions: RouteCraft、Memory、Collector、Dashboard
- benchmark v1: `mode=on|off`、`test_result`、`final_success`
- benchmark v2: v1の観測値に、同一比較対象を示すprivacy-boundedな`pair_id`と、比較条件を固定する`scope_id`を追加

全keyは固定allowlistで検証する。benchmark v1の4 keyは変更せず、v2は6 keyをexactに検証する。未観測値は`null`、`decision_source`は`routecraft | user | codex | fallback | unknown`のいずれかである。実行内容、prompt、session本文、絶対path、raw output、credentialは格納しない。Common Eventのtop-level identifierにも同じcredential拒否境界を適用し、値をerrorへ反射しない。Coreがlaneだけを選び、実modelをHostから観測できない場合、selected/actual modelを要求値から推測しない。legacyの`unknown-model`等のsentinelもactual実行として扱わない。

## Dashboard Impact projection

Impact集計は`decision_source=routecraft`と確認できたrunだけを対象にする。`user`、`codex`、`fallback`、`unknown`は帰属別に除外し、RouteCraft成果へ混ぜない。started/completed等のlifecycle eventは`(source, run_id)`で重複排除し、最新eventの観測値を優先する。

各Mixは独立した分母を持つ。requested model、actual model、requested reasoning、actual reasoningのどれかが欠けても、観測できた別Mixの分母からは除外しない。

- Route Changed: requested modelがactual modelと異なる、または同一modelでrequested/actual reasoningが異なる比較可能run。
- Sol Offload Rate: requested familyがSolでactual familyまで観測できたrunのうち、actual familyがSol以外の割合。
- Sol Executions Avoided: 上記Sol offloadの観測件数。金額やtoken節約量へ変換しない。
- Ultra Optimization Rate: requestedがSol Ultraで、actual familyが非Solならreasoning未観測でもoffloadとして比較可能にする。actual familyがSolの場合だけactual reasoningまで観測できたrunを比較可能とし、そのうちretained Ultra以外の割合。全requested件数、比較可能な分母、不足による除外数を別々に保持する。
- Ultra分類: retained、Sol reasoning reduced、Terra offload、Luna offload、other。
- Prompt Cache Hit Rate: `sum(cached_input_tokens) / sum(input_tokens)`。分母が正で両値を観測できたrunだけを使用し、Platform Efficiencyへ表示する。
- Memory Effect: `memory_recall_used`を観測できたrunに対するrecall-assisted率と、Case reuse / Rules appliedの件数。Useful Recallは評価証拠がなければ`null`。

反実仮想がないretry reduction、context reduction、repeated investigation avoided、token/time reduction、Routing Efficiency scoreは推測せず`null`またはwithheldと表示する。Level 1の観測回避数は`OBSERVED`、比較baselineがある推定だけを`ESTIMATED`、RouteCraft ON/OFFの再現可能な比較だけを`MEASURED`とする。

Route Matrixはfamily × reasoningの遷移ごとにruns、割合、token volume、execution timeを表示し、run ID、日時、task class、reason、confidence、tokens、duration、memory usedへ安全なallowlistでdrill-downする。

## Component manifestsとversion

Dashboardは`plugins/codex-routecraft/components/<component>/manifest.json`を実行時に読み、次を独立表示する。

- `routecraft-core`: Plugin manifestの`version`を`version_source`で参照
- `praxis-memory`
- `praxis-dashboard`
- `collector`
- `telemetry-schema`

Dashboard内へ他componentのversionを埋め込まない。manifest不在、schema不一致、path境界外、symlink、過大file、version形式不正は`unknown`とする。任意のbuild、commit、dateもmanifestにある安全な値だけを表示する。配布ZIPにはcomponent manifestsとPlugin manifestを同梱する。

## RouteCraft ON/OFF比較基盤

Telemetryはbenchmarkの`on | off`条件と、execution time、total/uncached/cached/output/reasoning tokens、model/tool/file calls、retry、test result、final successを同じprojectionへ載せられる。benchmark v1は既存の観測groupとして読み続けるが、比較identityがないため`MEASURED`へ昇格しない。v2では同じ`pair_id + scope_id`がON/OFFにexactly 1件ずつ存在する場合だけ比較pairとする。未pair、重複、identity欠落は除外数として残し、各metricは両側でその値を観測できたpairだけを同じ分母で集計する。uncached inputは各pairの両側でinputとcached inputを観測した場合だけ差分を算出する。十分な証拠がない間はDashboardを`MEASURED unavailable`のまま保ち、pairがあっても差分、削減率、因果効果、料金を自動推定しない。大量benchmarkも自動実行しない。

## 既存ユーザーの利用方法

既存コマンドは継続する。

```powershell
python plugins/codex-routecraft/scripts/routecraft.py --version
python plugins/codex-routecraft/scripts/routecraft.py doctor
python plugins/codex-routecraft/scripts/routecraft.py memory search "失敗"
python plugins/codex-routecraft/scripts/routecraft.py --json routing plan --task "互換確認" --mode advisory
python plugins/codex-routecraft/scripts/routecraft.py ui
```

分離コンポーネントは必要なものだけ起動できる。

```powershell
python plugins/codex-routecraft/scripts/routecraft-core.py --help
python plugins/codex-routecraft/scripts/praxis-memory.py --help
python plugins/codex-routecraft/scripts/praxis-dashboard.py --help
```

従来運用へ3つの個別起動を強制しない。既存`routecraft`をCompatibility Layerとして残し、段階的に新APIを利用する。

## Memory migration

### 方針

- 移行元は読み取り専用。
- dry-runが既定。
- applyには`--apply --confirm MIGRATE`が必要。
- targetが既に存在する場合は、apply前にSQLite backupを作る。
- 全入力を事前検証し、1 transactionで反映する。
- 同一source identityとcontent hashはskipする。
- 同一identityで内容が異なる場合はconflictとして既存値を保持する。
- 件数、integrity、schemaを前後で検証する。
- raw secretや絶対pathをreceiptへ保存しない。

Decision Storeはmarkerだけでなく各Markdown recordをcanonical schema v1 validatorへ通し、schema version、kind、ID prefix、配置先を検証する。candidateは未検証の仮説として`fact`へ移し、decisionへ昇格させない。破損・future schema・kind不一致はtarget作成前にfail closedする。

Memory Localの`title / body / project / tags / verified / source_ref`を移し、旧type、importance、active、関連file・commitの件数は互換tagとして残す。Praxisに同型fieldがないraw関連pathはコピーせず、元DBを変更しない正本として保持する。対応不能なsecret様データ、過剰なtag、未知schemaは黙って欠落させず、移行全体をfail closedする。

### RouteCraft Memory Localから

```powershell
python plugins/codex-routecraft/scripts/praxis-memory.py migrate-local `
  --input "C:\path\to\routecraft-local.sqlite3" `
  --data-dir "C:\path\to\praxis-data"

python plugins/codex-routecraft/scripts/praxis-memory.py migrate-local `
  --input "C:\path\to\routecraft-local.sqlite3" `
  --data-dir "C:\path\to\praxis-data" `
  --apply --confirm MIGRATE
```

### Markdown Decision Storeから

```powershell
python plugins/codex-routecraft/scripts/praxis-memory.py migrate-decision-store `
  --input "C:\path\to\routecraft-memory" `
  --project legacy

python plugins/codex-routecraft/scripts/praxis-memory.py migrate-decision-store `
  --input "C:\path\to\routecraft-memory" `
  --project legacy `
  --apply --confirm MIGRATE
```

移行完了後も元のDB、Markdown、Git履歴は変更・削除しない。rollbackは生成されたbackupと検証済み手順を使い、移行元へ書き戻さない。

## Security

- API key、token、private key、cookie、authorization値をMemory、Event、Dashboardへ保存・表示しない。
- Windows、UNC、任意rootのPOSIX絶対pathを共通Eventへ保存しない。
- SQLはparameter bindingを使う。
- source DB、Decision Store、archiveでsymlinkとpath traversalを拒否する。
- 未知schema version、破損DB、破損entryはfail closedし、部分反映を残さない。
- UIは`127.0.0.1`だけへbindし、Host、Origin、CSRF、CSP、JSON body上限を維持する。
- source文字列をHTMLとして解釈しない。
- arbitrary code、shell command、外部Model呼出しをMemory/Dashboardへ追加しない。

## Troubleshooting

### native routingがadvisoryへfallbackする

Registryの`native_routing`が`true`として確認されていない。未確認を有効と仮定せず、Hostの正式なcapability evidenceを登録する。

### Praxis Dashboardが「source unavailable」と表示する

Praxis Event sourceが未設定、または専用DBがまだ存在しない。既存Memory UIの障害ではない。Praxis Memoryを明示初期化するか、standalone Dashboardへdata directoryを指定する。

### migrationがdry-runのまま

安全のため既定では書かない。previewの件数、conflict、警告を確認してから`--apply --confirm MIGRATE`を明示する。

### migrationがunknown schemaで停止する

新しい形式を古いconverterで推測しない。現在のconverterが対応するschemaを確認し、対応版へ更新してから再実行する。

### 特殊イベントがRecallへ出ない

正常な既定動作である。benchmark、migration、stress等を確認する場合だけ`include_special_events`を明示する。

## 開発とテスト

Python 3.11以上を使用する。WindowsではUTF-8とbytecode抑止を明示すると、端末encodingとworking treeへの副作用を避けられる。

```powershell
$env:PYTHONUTF8 = '1'
$env:PYTHONDONTWRITEBYTECODE = '1'
python -B -X utf8 scripts/verify.py
python -B -X utf8 -m unittest discover -s tests -v
```

新コンポーネントの対象test:

```powershell
python -B -X utf8 -m unittest -v `
  tests.test_routecraft_protocols `
  tests.test_routecraft_core `
  tests.test_praxis_memory `
  tests.test_praxis_dashboard `
  tests.test_praxis_dashboard_server `
  tests.test_praxis_integration
```

受入れでは次も確認する。

- 既存319 testsの回帰
- CLI help、JSON、exit code、UTF-8
- dry-run/apply/backup/idempotency/corrupt input
- empty/missing/large/failed Dashboard state
- desktopと約375pxのoverflow、console error、focus、長い日本語
- diff全体、secret scan、source package収録

## 互換性とrollback

- 既存Plugin IDは`codex-routecraft@routecraft`のまま。登録を3つに増やさない。
- 6 Agent profileと`ROUTECRAFT PLAN`は変更しない。
- Graph IR v1、Graph Store、Graph Modeは変更しない。
- Memory Local schema 1とMarkdown schema 1は移行元として変更しない。
- Collector v1〜v4、Observatory schema 2、Control Center ingestは変更しない。
- 新しい専用DBを使用しない場合、既存運用へ戻すためのdata migrationは不要。
- rollback時も元データとPraxis backupを削除しない。

## Future roadmap

優先度順の候補:

1. 現行実行HostからEvent Schema v1を出す正式adapter。
2. Context Compilerへ渡す`retrieval -> ranking -> filtering -> context package` interfaceの実装。
3. Control Center側での新Event Schema adapter。既存v1〜v4 contractは削除しない。
4. Caseの繰返し成功をSkill Candidateとして提示するHuman Review UI。
5. 他社Agent adapter。正式仕様と検証可能なcapability evidenceが得られたものだけを追加する。
6. 任意の端末間同期。秘密・競合・復旧境界を別途設計してから導入する。

自動Skill昇格、未公開モデル固有実装、Cloud必須化、SaaS化、大規模message brokerは今回の範囲外である。
