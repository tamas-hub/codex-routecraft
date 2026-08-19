# Persistent Decision Layer V2/V3 運用ガイド

RouteCraft V2/V3は、Codexの外側に「過去の判断を次のセッションへ相続する層」を追加します。モデルのweightsを変更するものではなく、テストや公式情報の代わりでもありません。

狙いは、以前に一度解いた探索を、新しいチャットで毎回最初からやり直さないことです。

## 実装された機能

- **Recall**：現在の仕事に関連するRule・Case・Candidateだけを文字数上限内で取得
- **Learn**：検証済み作業をCase、未検証の傾向をCandidateとして保存
- **Promote**：複数案件で再現したCandidateだけをValidated Ruleへ昇格
- **Sync**：専用の非公開Gitリポジトリを使い、複数PC間で判断知能を同期

CLIはPython標準ライブラリだけで動きます。Gitが必要なのは同期機能です。

## 最重要：公開リポジトリへ個人知能を保存しない

`codex-routecraft`本体は公開リポジトリです。個人案件のroot cause、内部構造、非公開PRへのリンクなどを本体へ入れてはいけません。

そのため、CLIは初期状態ではプラグイン同梱ストアへの書き込みを拒否します。個人用には、必ず別の専用ストアを作ります。

## 1台だけで使う場合

```sh
python plugins/codex-routecraft/scripts/routecraft_memory.py init \
  --store ~/routecraft-memory \
  --git-init \
  --configure
```

Windows PowerShell：

```powershell
& .\plugins\codex-routecraft\scripts\routecraft-memory.ps1 init `
  --store "$HOME\routecraft-memory" `
  --git-init `
  --configure
```

設定は通常、`~/.codex/routecraft/memory.json`へ保存されます。

ストアの優先順位は以下です。

1. コマンドの`--store`
2. 環境変数`ROUTECRAFT_MEMORY_DIR`
3. 設定済みストア
4. プラグイン同梱の読み取り専用seed

## 複数PCで共有するV3構成

最初にGitHub上で**空のPrivate Repository**を1つ作成します。アプリのソースリポジトリとは分離してください。

### 1台目

```sh
python plugins/codex-routecraft/scripts/routecraft_memory.py init \
  --store ~/routecraft-memory \
  --git-init \
  --remote git@github.com:OWNER/PRIVATE-MEMORY-REPO.git \
  --configure \
  --auto-sync both

python plugins/codex-routecraft/scripts/routecraft_memory.py sync
```

### 2台目以降

```sh
python plugins/codex-routecraft/scripts/routecraft_memory.py init \
  --store ~/routecraft-memory \
  --clone git@github.com:OWNER/PRIVATE-MEMORY-REPO.git \
  --configure \
  --auto-sync both
```

各PCにはローカル設定としてdevice IDが付与されます。記録IDはUTC時刻・device ID・乱数を組み合わせるため、別PCで同時に記録してもファイル名が衝突しにくい構造です。

共有リポジトリで管理するのは、主に以下です。

- `cases/`
- `candidates/`
- `rules/`
- `.routecraft-store.json`
- ストア用README

検索用インデックスとロックファイルは`.routecraft/`に置き、Gitでは共有しません。中央のINDEXファイルを各PCが同時編集して衝突する問題を避けるためです。

## Recall：過去知能を必要分だけ取得

```sh
python plugins/codex-routecraft/scripts/routecraft_memory.py recall \
  --query "再起動後に学習履歴が消える" \
  --tag 永続化 \
  --limit 5 \
  --budget 12000
```

JSON出力：

```sh
python plugins/codex-routecraft/scripts/routecraft_memory.py recall \
  --query "再起動後に学習履歴が消える" \
  --json
```

日本語は文字n-gramでも検索します。検索対象はID、タイトル、タグ、適用範囲、証拠、本文です。

ただし、52万文字や111万文字をそのままコンテキストへ入れるわけではありません。CLI内部のローカルインデックスで検索し、上位数件の判断に必要な部分だけを返します。

- Rule：Decision、適用条件、検証方法を優先
- Case：Root cause、Reusable lesson、Verificationを優先
- Candidate：Observation、不確実性、昇格条件を優先

## Learn：仕事の結果を外部知能へ残す

作業完了後、Codexは検証済みの内容だけをJSONパケットにします。

```json
{
  "kind": "case",
  "title": "アプリ再起動後に学習履歴が消える",
  "tags": ["expo", "ios", "永続化"],
  "scope": ["react-native"],
  "repository": "owner/repository",
  "outcome": "fixed",
  "sections": {
    "Problem": "再起動後に進捗が消えた。",
    "Root cause": "状態を一時キャッシュへ保存していた。",
    "Failed approaches": "当初はOSアップデートだけを疑っていた。",
    "Fix": "永続ストレージへ移した。",
    "Verification": "アプリを2回再起動し、回帰テストを実行した。",
    "Reusable lesson": "OSを疑う前に保存境界を確認する。"
  },
  "candidate": {
    "title": "実行環境を疑う前に保存先の永続性を確認する",
    "tags": ["debugging", "永続化"],
    "scope": ["mobile"],
    "sections": {
      "Observation": "一時保存はOS不具合と似た症状を作る。",
      "Possible decision value": "ストレージadapterを早期確認する。",
      "Counterexamples / uncertainty": "migration失敗でも同じ症状は起こり得る。",
      "Promotion condition": "別リポジトリでも再現すること。"
    }
  }
}
```

保存：

```sh
python plugins/codex-routecraft/scripts/routecraft_memory.py learn \
  --input /tmp/routecraft-learning.json \
  --sync
```

CaseとCandidateを同時作成できます。また別案件で同じCandidateが観測された場合は、`reinforce_candidates`へCandidate IDを指定します。

新しいCase IDが証拠へ追加され、重複していない場合だけ観測回数が増えます。昇格条件を満たしたCandidateは、コマンド結果の`eligible_for_promotion`に出ます。

## Promote：再現したものだけをRuleへ昇格

通常の昇格条件は以下です。

- 観測回数2回以上
- ストア内に保存された異なるCase 2件以上
- Decision文と適用条件・検証方法を記述

```json
{
  "candidate_id": "CAND-20260819T120000Z-MAC-A1B2",
  "title": "実行環境を疑う前に永続化境界を確認する",
  "decision": "再起動後に状態が消える場合、OSやruntimeを疑う前に保存先とdurability contractを検証する。",
  "when_to_apply": "再起動、suspend、reboot後に状態が消える場合。",
  "when_not_to_apply": "serializationまたはmigration失敗が既に証明されている場合。",
  "rationale": "mobileとdesktopの独立案件で一時領域が同じ症状を作った。",
  "verification": "プロセス再起動と実際の永続保存先確認を行う。"
}
```

```sh
python plugins/codex-routecraft/scripts/routecraft_memory.py promote \
  --input /tmp/routecraft-promotion.json \
  --sync
```

公式仕様など1件の非常に強い証拠だけで昇格する例外経路もありますが、`--authoritative`と`--human-approved`の両方が必要です。AIが単独でこの例外経路を使ってはいけません。

## Syncの動作

```sh
python plugins/codex-routecraft/scripts/routecraft_memory.py sync --mode both
```

`both`は以下を行います。

1. 記録形式と秘密情報を検証
2. 記憶ストア直下で許可されたMarkdown記録・templateだけをstage
3. 変更があればcommit
4. remote branchがあればpull --rebase
5. push。競合しない更新なら一定回数再試行
6. ローカル検索インデックスを再構築

`pull`はcleanなストアでのみ実行できます。`push`はcommitとpushを行い、push rejection時にpull --rebaseを試します。

同期対象は**専用Gitリポジトリのroot**でなければなりません。製品リポジトリのサブフォルダを誤って記憶ストアにした場合、syncは拒否します。

## 安全策

- 典型的なtoken、API key、private keyを検出して保存拒否
- raw log、全文会話、秘密情報は保存対象外
- stage対象を記憶用ディレクトリ直下のMarkdown記録・templateへ限定
- store sentinelがない場所では動作拒否
- symlink、非Markdown payload、Git remote-helper構文、巨大な記録本文を拒否
- 同一PC内の同時書き込みをlockで抑制
- CandidateとValidated Ruleを明確に分離
- 現在のコード、テスト、公式情報が常に過去知能より優先

## 運用コマンド

```sh
# 設定、件数、昇格候補、Git状態
python plugins/codex-routecraft/scripts/routecraft_memory.py status --json

# 全記録を検証
python plugins/codex-routecraft/scripts/routecraft_memory.py validate

# ローカル検索インデックス再生成
python plugins/codex-routecraft/scripts/routecraft_memory.py reindex

# 人間確認用INDEX.mdも生成
python plugins/codex-routecraft/scripts/routecraft_memory.py reindex --markdown
```

## V3でまだ断定しないこと

この仕組みによって、prompt cache率や週間quotaが何％改善するかは断定しません。直接減らす対象は、同じ検索、同じ失敗、同じ仮説検証を繰り返す再計算です。

評価する場合は、cache率だけでなく、経過時間、tool call数、外れ仮説数、再作業量、成果品質を分けて測定してください。
