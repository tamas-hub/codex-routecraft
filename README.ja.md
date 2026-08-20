# RouteCraft for Codex

RouteCraftは、**Solを設計・統合・最終判断に残し、実装だけを必要に応じてLuna/Terraへ振り分け、過去に獲得した判断を次のCodexへ相続する**オーケストレーション・プラグインです。

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
codex plugin marketplace add tamas-hub/codex-routecraft --ref main
codex plugin add codex-routecraft@routecraft
```

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

## License

MIT
