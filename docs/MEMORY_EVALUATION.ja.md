# RouteCraft Memory 評価ガイド

RouteCraft Memoryは「記憶できた」だけでは有効性を証明できません。この評価層は、Memoryが実際にCodexの再調査を減らし、正しい判断を早く取り出し、別プロジェクト・別端末へ知見を転送できているかを測るためのものです。

## 基本思想

評価は次の3つを分離します。

1. **機能が正常か** — 保存、Recall、同期、別端末継承、Secret拒否など。
2. **検索品質が良いか** — Hit@K、Precision@K、Recall@K、MRR。
3. **開発成果が改善したか** — 所要時間、Tool Call、外れ仮説、再利用、誤誘導。

Memory件数が増えたこと自体を成果指標にはしません。

## ローカル評価ログ

評価ログはデフォルトで次に保存されます。

```text
~/.codex/routecraft/evaluation/
├─ config.json
├─ events.jsonl
└─ benchmark-last.json
```

このディレクトリはDecision Storeではなく**各端末のローカル専用**です。GitHubへ同期しません。

保存しないもの:

- raw prompt / recall query
- 会話全文
- source code
- console log全文
- token / password / credential / private key
- `C:\Users\...` や `/Users/...` などの絶対ユーザーパス

Observatoryへ送る場合も、送信するのは集計値だけです。

## 有効化

通常計測を有効化します。

```sh
python plugins/codex-routecraft/scripts/routecraft_evaluation.py \
  configure --enable --mode full --json
```

無効化:

```sh
python plugins/codex-routecraft/scripts/routecraft_evaluation.py \
  configure --disable --json
```

評価はopt-inです。無効でもRouteCraft本体、Decision Memory、Orchestrationは従来どおり動きます。

## 3モード

### off

Persistent Memoryを使わないbaselineです。

- Recallしない
- Learnしない
- RouteCraftの計画、Agent Routing、Verificationは通常どおり

### recall

既存Memoryの検索効果だけを測ります。

- Recallする
- Learnしない

### full

通常運用です。

- Recallする
- 検証後にCase/CandidateをLearnする
- Promotion gateは従来どおり

## A/B/C試験

意図的にbaselineを集めたい期間だけRound Robinを有効にできます。

```sh
python plugins/codex-routecraft/scripts/routecraft_evaluation.py \
  configure \
  --enable \
  --experiment round-robin \
  --sequence off recall full \
  --json
```

以後、substantive task開始時に `off → recall → full → off ...` と割り当てます。

緊急案件、高リスク案件、Memoryなしで品質を落としたくない案件では実験を有効にしないでください。

## 1タスクの計測

Orchestration Skillから自動的に使うことを想定しています。

開始:

```sh
python plugins/codex-routecraft/scripts/routecraft_evaluation.py \
  start --repo-path . --task-class debugging --risk low --json
```

返却例:

```json
{
  "tracking": true,
  "task_id": "EVAL-...",
  "mode": "full",
  "repository": "owner/repository"
}
```

Recall後は**検索文字列ではなく、返ったRecord IDと順位だけ**を記録します。

```sh
python plugins/codex-routecraft/scripts/routecraft_evaluation.py \
  recall \
  --task-id EVAL-... \
  --store ~/routecraft-memory \
  --record CASE-...:1 \
  --record RULE-...:2 \
  --json
```

終了時はParent Verification後に判定します。

```sh
python plugins/codex-routecraft/scripts/routecraft_evaluation.py \
  finish \
  --task-id EVAL-... \
  --outcome success \
  --useful-record CASE-... \
  --stale-record RULE-... \
  --learned-record CASE-... \
  --json
```

### Recall結果の判定

- **useful**: 正しい調査経路、原因特定、実装、検証を実質的に短縮・改善した。
- **misleading**: 過去Memoryが誤った方向へ誘導した。
- **stale**: 現在のコード・仕様・テストと矛盾し、古い知識として正しく棄却した。
- 指定しないRecord: neutral / 実質的な影響なし。

`tool_calls`、`failed_hypotheses`、文字数などは**観測できる時だけ**記録します。AIが推測して数字を作ることは禁止です。

## Live指標

`summary`で確認します。

```sh
python plugins/codex-routecraft/scripts/routecraft_evaluation.py summary --json
```

主な指標:

- Completed Tasks
- Recall Tasks
- Useful Task Rate
- Observed Precision
- MRR Useful
- Misleading / Stale件数
- Cross-project Useful
- Cross-device Useful
- `off`対`recall` / `full`の所要時間
- Tool Call削減率
- Failed Hypothesis削減率
- Estimated Saved Seconds
- Decision Compression Ratio
- Privacy Violations

### Useful Task Rate

Recallしたタスクのうち、1件以上のMemoryが実際に役立った割合です。

### Observed Precision

返されたRecordのうち、Parent Verification後に`useful`と判定された割合です。

これは標準的な検索評価用Precision@Kとは別です。実運用での有用性フィードバックです。

### MRR Useful

最初に役立ったRecordが何位に出たかを評価します。

1位なら1.0、2位なら0.5、3位なら約0.33です。

### Cross-project / Cross-device Useful

保存元RepositoryやDeviceが現在タスクと異なるRecordが`useful`だった回数です。

これが増えるほど、Memoryが単なるRepo内キャッシュではなく、外部知能として転送されていることを示します。

## 検索ベンチマーク

標準的なHit@K / Precision@K / Recall@K / MRRを測る場合は、正解Record IDを持つローカルbenchmark suiteを用意します。

例:

```json
{
  "schema_version": 1,
  "cases": [
    {
      "name": "private-email-push",
      "query": "Git push rejected private email",
      "tags": ["git", "github"],
      "expected": ["CASE-..."]
    }
  ]
}
```

実行:

```sh
python plugins/codex-routecraft/scripts/routecraft_evaluation.py \
  benchmark \
  --store ~/routecraft-memory \
  --suite ~/.codex/routecraft/evaluation/benchmark.json \
  --limit 5 \
  --json
```

SuiteにはPrivateな検索文が含まれる可能性があるためGitへ上げません。`benchmark-last.json`へ保存されるのは集計値だけです。

## Decision Compression Ratio

元の作業コンテキスト量と最終Memory Record量を観測できる場合だけ記録します。

```text
1 - (Memory Record chars / Source chars)
```

例えば80,000文字の作業から2,000文字のCaseを残した場合は97.5%の圧縮です。

ただしこの値を上げるために必要なVerificationやRoot Causeを削るのは本末転倒です。

## RouteCraft Memory Score

Scorecardは次の配点を持ちます。

| 項目 | 配点 |
|---|---:|
| Retrieval Quality | 20 |
| Task Time Reduction | 20 |
| Failed Hypothesis Reduction | 15 |
| Cross-project Transfer | 10 |
| Cross-device Transfer | 10 |
| Memory Correctness | 10 |
| Stale Resistance | 5 |
| Privacy Integrity | 5 |
| Decision Compression | 5 |
| 合計 | 100 |

重要なのは**Coverage**です。

データが足りない項目は0点扱いせず未評価になります。評価可能な配点が50%未満なら、100点Score自体を表示しません。

状態:

- `insufficient-data`: Coverage < 50%
- `provisional`: Coverage >= 50% だがCompleted Task < 20
- `established`: Coverage >= 50% かつCompleted Task >= 20

少数サンプルで「Memory Score 100」と宣伝することを防ぐ設計です。

## 初期評価の目安

### Case 0〜10

機能・同期・検索のSmoke Test中心。

### Case 20〜30 / measured task 10〜20

最初のUseful Rate、MRR、Cross-device reuseが見え始めます。

### Case 50〜100 / measured task 30以上

`off` baselineが十分あれば、時間短縮・外れ仮説削減・Cross-project transferを評価しやすくなります。

## Observatory連携

`routecraft_observatory.py`はEvaluatorの`summary --compact`を取得し、次のような**集計値だけ**をHeartbeatへ追加できます。

- completed_tasks
- recall_tasks
- useful_task_rate
- observed_precision
- mrr_useful
- cross_project_useful
- cross_device_useful
- estimated_saved_seconds
- decision_compression_ratio
- privacy_violations
- score / coverage / status
- benchmark aggregate

Repository名、Task ID、Record ID、検索文、絶対パスはObservatoryへ送りません。

## 評価の限界

- タスク難易度が違えば単純な時間比較はできません。
- `off`と`full`を完全に同じ開発課題で比較することは通常できません。
- Agentやモデル更新も結果へ影響します。
- Useful判定にはParent側の判断が含まれます。

そのため、Scoreだけでなく**サンプル数、Coverage、Mode別統計、Benchmark**を一緒に見ることを推奨します。
