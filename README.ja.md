# RouteCraft for Codex

RouteCraftは、**Solを設計・統合・最終判断に残し、実装だけを必要に応じてLuna/Terraへ振り分け、過去に獲得した判断を次のCodexへ相続する**オーケストレーション・プラグインです。

v0.7.xの互換入口を維持しながら、責務を段階的に3つへ分離しています。

| コンポーネント | 責務 | 単独利用 |
| --- | --- | --- |
| RouteCraft Core | Routing Hint、Host Capability、実行制御 | Memoryなしで可 |
| Praxis Memory | Facts / Cases / Decisions / Failures / Experience / Events | Coreなしで可 |
| Praxis Dashboard | Runtime / Routing / Memory / Usage / Eventsのsource-neutral表示 | Coreなしで可 |

3者は[Common Event Schema v1と分離アーキテクチャ](docs/PRAXIS-ARCHITECTURE.ja.md)で接続します。既存の`routecraft`、Graph IR v1、RouteCraft Memory Local、Markdown Decision Store、Collector v1〜v4はCompatibility Layerとして残り、Praxisの専用SQLiteへ自動移動されません。

## RouteCraft Local Runtime 0.7.4

このrepositoryには、既存のMarkdown Decision Storeとは独立したローカル製品「RouteCraft Memory Local」も含まれます。

> 昨日のAI開発の続きを、今日のAIに正確に引き継ぐ。

プロジェクトごとの判断、失敗、制約、重要file、次の作業をSQLiteへ構造化して保存し、外部APIなしで検索、Context Pack、Handoff Packを生成します。既存の`routecraft_memory.py`、Case/Candidate/Rule、Private Git同期は変更せず、明示importで移行できます。

必要なものはPython 3.11以上だけです。Git状態を使う場合だけGit CLIを使用します。

```powershell
$env:PYTHONUTF8 = '1'
python plugins/codex-routecraft/scripts/routecraft.py init
python plugins/codex-routecraft/scripts/routecraft.py project add --name "サンプル開発" --repo "C:\path\to\repo"
python plugins/codex-routecraft/scripts/routecraft.py project list
python plugins/codex-routecraft/scripts/routecraft.py loop configure --enable
python plugins/codex-routecraft/scripts/routecraft.py ui
python plugins/codex-routecraft/scripts/routecraft-core.py --help
python plugins/codex-routecraft/scripts/praxis-memory.py --help
python plugins/codex-routecraft/scripts/praxis-dashboard.py --help
```

専用Dashboardはloopback限定・GET限定の観測入口です。既存の`praxis-events.jsonl`または`praxis-memory.sqlite3`だけを読み、sourceがない場合にRouteCraft Memory Localや新規DBを初期化しません。OverviewはSystem Status、RouteCraft Impact、Execution、Platform Efficiencyを分離し、requested→actual、Sol offload、Ultra optimization、Memory evidence、component manifest versionを実ログから表示します。Prompt CacheはOpenAI / Codex側の指標として分離し、未観測値やA/B比較未実施の効果を推測しません。A/Bはbenchmark v2の同一`pair_id + scope_id`がON/OFFに1件ずつ揃う場合だけMEASURED evidenceとして扱い、旧v1・未pair・重複は観測値のまま比較対象外にします。

macOS/Linuxでは`python`を`python3`へ読み替えます。既定dataは`~/.routecraft-memory-local/`です。Web UIは`127.0.0.1`だけで起動し、Memory Local本体はtelemetry、AI API、外部assetを使用しません。

主な操作:

```text
routecraft project add|list|show|edit|archive|delete|backup|restore
routecraft memory add|list|edit|search|import|export
routecraft context build
routecraft handoff build
routecraft git status
routecraft session summarize
routecraft loop status|configure
routecraft backup|restore|doctor|ui
routecraft doctor --scope health|all
routecraft collector collect
routecraft routing plan|capabilities
routecraft graph plan|validate|run|resume|status|cancel|export
routecraft policy status|candidates
routecraft context engine --project <ID>
routecraft agents analyze|preview|apply
routecraft security analyze|preview|apply
routecraft benchmark
routecraft update --apply
routecraft migrate local-db|decision-store|endpoint
```

既存MemoryをPraxisへコピーする場合は、まずdry-runで件数とconflictを確認し、`--apply --confirm MIGRATE`を明示します。移行元は変更せず、既存targetがある場合は事前backupを作成します。詳細は[Migration手順](docs/PRAXIS-ARCHITECTURE.ja.md#memory-migration)を参照してください。

0.7.4は、0.7のGraph IR v1、fail-closed compiler、Evidence Gate、専用SQLite Graph State Store、hash-chain checkpoint、resume、Selective Retry、Verified Constraint、Policy Labを維持します。加えて、安全なReal Agent Benchmarkのcredential分離を証明できないnative Windowsでは、認証参照・credential複製・UAC/helper setup・artifact作成・model呼出しより前にfail closedします。deterministic benchmark、Graph observe、Doctor、Security validation、0.6 Single Node Fast PathはWindowsでも継続します。Real Agent runはmacOS、Linux、利用可能なWSL2または専用VMでmodel-free isolation preflightに合格した場合だけ許可します。既定modeは`observe`で、実証済みtask classだけをallowlistで`enforce`できます。Unified Collectorはschema v1〜v3を維持し、v4のprivacy-safe Graph／Benchmark／Security集計だけを追加します。prompt・会話・source／file本文・Memory／Decision本文・path・credential・raw node outputは送信しません。取得不能値はlocal／schema v4 evidenceでは`null`であり、0へ置換しません。legacy D1 schema v3の`benchmark_runs`はmetric列が`NOT NULL`のため、取得不能summaryはlegacy row自体を送らず、偽の0を作りません。`CONTROL_CENTER_ENABLED`が未設定またはfalseでもLocal Runtimeは単独で動きます。

### 製品境界とSource of Truth

Local RuntimeとControl Center Add-onは、別repository・別package・別version・別licenseで配布できる境界を維持します。schema v1〜v4のversioned JSON contractだけが両者を接続し、schema v4はv1〜v3を削除せずHardening / Graph evidenceの集計だけを追加します。Control Center停止・未契約・通信障害はLocal Runtimeの処理結果に影響しません。

| 領域 | Source of Truth | 永続状態 | 外部表示 |
| --- | --- | --- | --- |
| Routing / Hooks / Agents / Collector | `codex-routecraft` | 端末ローカル設定 | optional schema v1〜v4 summary |
| Project Memory | RouteCraft Memory Local | local SQLite | aggregate counts only |
| Reusable Decision | Private Decision Store | separate private Git store | aggregate counts only |
| Benchmark / Security | local engine + adapter | local report | aggregate summary only |
| Control Center UI / API / D1 | Control Center repository | existing Sites D1 | owner-only Site |

Memory LocalとDecision Storeを物理統合せず、`Context Engine`がadapter経由で必要な項目だけをranking・deduplication・budget compilationします。

AGENTS optimizerとSecurity Hardenerは`analyze`→`preview`→明示`apply`の順です。既定では書き換えません。`update`、DB migration、Decision Store importは明示確認が必要で、既存bootstrap/初期化実装へ委譲します。trayのURL移行は`routecraft migrate endpoint --config ... --old-url ... --new-url ...`でdry-runし、`--apply --confirm APPLY`時だけendpoint値だけをatomicに変更します。token、interval、enabled/OFF状態は保持し、tokenは出力しません。

Security Hardenerの静的検査は、Git追跡済み（非Git時は上限付き）のテキストだけを読み、symlink/junction、依存物、生成物、巨大ファイルを追跡しません。secretらしき値やソース本文は出力せず、code・相対ファイル・行・安全な推奨だけを返します。依存lock、GitHub Actions、CSP/CORS/auth、Cloudflare設定、logging、unsafe eval/shell/SQL、infra設定をローカル観測します。外部脆弱性監査は実行せず、auth/authorization/SQL/主要依存の修正は推奨のみです。baseline fingerprintを入力すると既存・新規・解消件数を比較できます。Control Center向けsummaryは集計値のみです。

Loop連携は明示的に有効化した場合だけ、登録済みprojectのCompact ContextをSessionStartへ注入し、正常なStop時にread-only Git metadataから未確認のsession summaryを保存します。raw transcriptは読まず、projectを自動作成しません。Decision Storeは汎用Case/Rule、Memory Localはproject作業記憶として分離します。反映には新しいCodexタスクが必要です。

0.7の設計判断は[ADR-0007](docs/ADR-0007-EVIDENCE-DRIVEN-DURABLE-GRAPH.ja.md)、実装契約は[RouteCraft 0.7 Architecture](docs/ROUTECRAFT-0.7-ARCHITECTURE.ja.md)を参照してください。0.6 foundation、デモ、配布ZIP、復元、既知制約も引き続き保持します。

狙いは「サブエージェントを大量に使うこと」でも「巨大プロンプトを毎回読むこと」でもありません。

**小さい仕事はSol単独。委譲するなら最安で完遂できるlane。並列化は独立作業だけ。最後は親Solがdiffとテストを確認。高リスク時だけFresh Solレビュー。仕事後は検証済み知見だけを外部知能へ残す。**

## V0.5.1で追加されたもの

- Windows Observatory heartbeatを、画面を出さないタスクトレイ常駐として実行
- 緑＝ON、灰＝OFF、橙＝送信エラーのミニアイコンと右クリック操作
- ログオン時に1回だけ起動し、5分ごとのスケジュールタスクを作らない自動起動方式
- 明示的な許可なしにはheartbeatや自動起動を登録しない運用契約

## V0.5.0で追加されたもの

- RouteCraft Memoryが本当に役立っているかを測るopt-inのローカル評価層
- `off` / `recall` / `full` の3モードと、任意のRound-Robin A/B/C試験
- RecallしたRecordを検証後に`useful` / `misleading` / `stale`として評価
- Useful Recall Rate、Observed Precision、MRR、Cross-project / Cross-device reuseを集計
- Memory有無による時間、Tool Call、外れ仮説の比較
- Decision Compression Ratioと推定削減時間
- Hit@K / Precision@K / Recall@K / MRRのローカル検索ベンチマーク
- データ不足時は100点評価を出さないCoverage-aware RouteCraft Memory Score
- Observatory heartbeatへ、検索文・Repository名・Record ID等を含まない集計値だけを連携

評価ログは`~/.codex/routecraft/evaluation/`に端末ローカルで保存され、Private Decision Storeへ同期されません。詳細は[RouteCraft Memory 評価ガイド](docs/MEMORY_EVALUATION.ja.md)を参照してください。

## V0.4.0で追加されたもの

- すべてのCodexタスクへprivate-by-defaultのGitHub原本ポリシーを渡すSource Guard
- タスク開始前のdirty状態を保護し、今回生じた未commit／未pushだけをStop時に検出
- raw transcript、`.env`、DB、upload、cache、端末設定をGitHub対象から除外
- 3端末で共通実装を使い、GitHub ownerなどは端末ローカル設定として保持

## V0.3.0で追加されたもの

- 作業前に関連Rule・Caseだけを検索する`recall`
- 検証済みCaseと未検証Candidateを保存する`learn`
- 複数案件で再現したCandidateだけをRuleへ昇格する`promote`
- Private Gitリポジトリを使った複数PC同期`sync`
- 秘密情報検出、専用Git root確認、ローカルlock、昇格gate

## 全体構成

```text
Private Decision Store
Rules / Cases / Candidates
          ↓ 必要な数件だけRecall
Sol / High（設計・最終判断）
  ├─ SOLO
  ├─ DELEGATE → Luna / Terra
  ├─ PARALLEL → 独立した2～3作業
  ├─ Parent verification
  └─ 高リスク時のみ Fresh Sol review
          ↓ 検証済みLearning Packet
Case → Candidate → Validated Rule
          ↓
Local Evaluation（opt-in）
有用性 / 誤誘導 / 再利用 / 時間短縮を計測
```

## 重要な原則

- `solo`を標準にして委譲オーバーヘッドを抑える
- 子と親で同じ実装を重複しない
- 親Solが実diffとテストを再確認する
- 過去知能は「証拠」であり、現在のコードやテストより上位ではない
- 1回の成功をいきなりRuleにしない
- 常時読む情報は小さくし、必要な記録だけ取り出す
- 個人知能は公開ソースリポジトリと分離する
- Memory件数そのものを成果とせず、実際の再利用・短縮・正確性を測る

## 導入

```sh
codex plugin marketplace add tamas-hub/codex-routecraft --ref v0.7.4
codex plugin add codex-routecraft@routecraft
```

別PCへは、監査済みcommitから生成したWindows／macOS starter ZIPを推奨します。固定checkoutを検証してread-only planを表示し、`INSTALL`の明示確認後だけlocal transactionを実行します。可変な`main`は開発・未リリース検証専用です。

Custom Agentの追加手順は英語READMEまたは`docs/INSTALL.md`を参照してください。

## 個人用Decision Storeの作成

プラグイン同梱ストアは公開・読み取り専用seedです。個人案件の知能を書き込むことは初期状態で拒否されます。

1台だけで使う場合：

```sh
python plugins/codex-routecraft/scripts/routecraft_memory.py init \
  --store ~/routecraft-memory \
  --git-init \
  --configure
```

複数PCで共有する場合は、先にGitHub上で**空のPrivate Repository**を作成します。

1台目：

```sh
python plugins/codex-routecraft/scripts/routecraft_memory.py init \
  --store ~/routecraft-memory \
  --git-init \
  --remote git@github.com:OWNER/PRIVATE-MEMORY-REPO.git \
  --configure \
  --auto-sync both

python plugins/codex-routecraft/scripts/routecraft_memory.py sync
```

2台目以降：

```sh
python plugins/codex-routecraft/scripts/routecraft_memory.py init \
  --store ~/routecraft-memory \
  --clone git@github.com:OWNER/PRIVATE-MEMORY-REPO.git \
  --configure \
  --auto-sync both
```

詳細は[Persistent Decision Layer日本語ガイド](docs/PERSISTENT_DECISION_LAYER.ja.md)を参照してください。

複数端末とSource Guardの導入は[複数端末フリート運用](docs/MULTI_DEVICE_FLEET.ja.md)を参照してください。

## 使い方

Sol / Highで新しいタスクを開始し、例えば次のように指示します。

```text
Use $codex-routecraft:orchestration to implement this task. Recall relevant prior decisions, choose the cheapest safe lane, parallelize only independent work, verify the complete diff, and capture reusable verified learning.
```

Store管理だけを行う場合は、次のskillも利用できます。

```text
Use $codex-routecraft:memory to inspect, validate, recall, or synchronize my private RouteCraft decision store.
```

## 指示時と実動のテレメトリ

`routecraft_telemetry.py`は、Codexのローカルrolloutから、**人間がタスクを開始した親モデル／推論レベル → 実際に動いた子モデル／推論レベル**を対応付けます。実行回数、入力、cache入力、出力、推論出力、総トークン、経過時間も収集します。schema v2では、Assistantが厳密なマーカーで明示した固定task class、匿名化済み短文、Recall／有用判定／Learn状態だけを追加できます。ユーザーpromptからの自動要約、path、ファイル名、Record ID、credential、raw session IDは送信しません。

会話本文、作業ディレクトリ、ファイル名、Agentのtask path、raw session IDは出力しません。端末ID、親子session IDは端末固有saltでハッシュ化します。

ローカル確認：

```sh
python plugins/codex-routecraft/scripts/routecraft_telemetry.py --print
```

旧Orchestratorの履歴も一度だけ取り込む場合：

```sh
python plugins/codex-routecraft/scripts/routecraft_telemetry.py \
  --include-legacy --since-days 0 --output routecraft-telemetry.json
```

HTTPS endpointへ送信する場合は、32文字以上のBearer tokenを別ファイルに保存して`--endpoint`と`--token-file`を指定します。既定は直近30日で、旧Orchestratorのroleは除外します。
非公開GPT Sitesの入口認証を併用する場合は、収集API用tokenとは別のSites bypass tokenファイルを`--sites-bypass-token-file`で指定できます。既存のObservatoryトレイへ組み込む場合は、installerの`TelemetryEndpoint`、`TelemetryTokenFile`、`TelemetrySitesBypassTokenFile`を使います。トレイをOFFにするとHeartbeatとテレメトリの両方が停止し、OFF状態は再インストール後も維持されます。

### Recall

```sh
python plugins/codex-routecraft/scripts/routecraft_memory.py recall \
  --query "再起動後に学習履歴が消える" \
  --tag 永続化 \
  --limit 5 \
  --budget 12000
```

52万文字、111万文字へ増えても全部をプロンプトに入れません。ローカルインデックスで検索し、RuleならDecisionと適用条件、CaseならRoot causeとVerificationなど、必要な部分だけを返します。

### Learn

```sh
python plugins/codex-routecraft/scripts/routecraft_memory.py learn \
  --input docs/examples/case-packet.json
```

検証済みの事実はCaseへ、別案件でも再現する可能性がある傾向はCandidateへ保存します。raw log、会話全文、秘密情報、コピーしただけのソースコードは保存しません。

### Promote

```sh
python plugins/codex-routecraft/scripts/routecraft_memory.py promote \
  --input docs/examples/promotion-packet.json
```

通常は観測2回以上＋ストア内に保存された異なるCase 2件以上が必要です。公式仕様1件などの例外経路は、人間の明示承認なしにAIだけで使えません。

### Sync

```sh
python plugins/codex-routecraft/scripts/routecraft_memory.py sync --mode both
```

記憶用に許可されたファイルだけをcommitし、pull --rebase、push、ローカルindex再生成を行います。製品リポジトリのサブフォルダを誤ってstoreにした場合は同期を拒否します。

## Memoryの効果を測る

評価機能は初期状態で無効です。通常運用を計測する場合：

```sh
python plugins/codex-routecraft/scripts/routecraft_evaluation.py \
  configure --enable --mode full --json
```

その後のsubstantive taskではOrchestration Skillが、利用可能であれば開始・Recall・検証後の評価をローカルに記録します。

A/B/C試験を明示的に行いたい期間だけ、次のように設定できます。

```sh
python plugins/codex-routecraft/scripts/routecraft_evaluation.py \
  configure --enable --experiment round-robin \
  --sequence off recall full --json
```

集計：

```sh
python plugins/codex-routecraft/scripts/routecraft_evaluation.py summary --json
```

検索品質だけを測るローカルbenchmarkも利用できます。少数サンプルで効果を誇張しないため、評価可能な配点Coverageが50%未満なら100点Scoreは表示しません。

## コストとcacheについて

固定の「○％削減」や「cache率○％」はうたいません。

この仕組みが直接減らすのは、同じ検索、同じ失敗、同じ仮説検証を毎回やり直す再計算です。0.5.0以降は、経過時間、Tool Call、外れ仮説、Recallの有用性、Cross-project / Cross-device reuseなどを観測可能な範囲で記録して評価できます。

## セキュリティ

RouteCraftはsecurity boundaryではありません。ただしmemory CLIと評価層には以下を実装しています。

- 典型的なtoken、API key、private keyの保存拒否
- 記憶用ディレクトリ直下のMarkdown記録・templateだけをGit stage
- store sentinel確認
- symlink、Git remote-helper構文、巨大な記録本文を拒否
- 専用Git repository root確認
- 同一PC内の同時書き込みlock
- CandidateとValidated Ruleの分離
- Evaluation Eventへraw prompt / query / transcript / source code / credentials /絶対ユーザーパスを保存しない
- Evaluation EventはDecision Storeへ同期せず端末ローカルに保持
- Observatoryへは評価集計値のみ送信
- 実行テレメトリへはハッシュ化した親子run、role、model、effort、時刻、token集計だけを送信し、会話本文・作業path・ファイル名・raw session IDを含めない

## License

MIT
