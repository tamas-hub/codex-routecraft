# RouteCraft Memory Local v1.0 製品仕様

## 目的

> 昨日のAI開発の続きを、今日のAIに正確に引き継ぐ。

RouteCraft Memory Localは、AI開発のchat履歴を丸ごと保存する製品ではない。プロジェクトごとの設計判断、失敗、制約、重要ファイル、変更、次の作業を構造化し、次のsessionに必要な範囲だけをローカル検索してContext PackまたはHandoff Packへ変換する。

## 想定利用者と成功状態

- Codex、Claude Code、Cursor、GitHub Copilot等を複数sessionまたは複数製品で使う個人開発者。
- アカウント、有料API、cloud server、Dockerなしで導入できる。
- READMEだけでproject登録、memory登録、検索、Context/Handoff、backupを実行できる。
- 利用者のcode、DB、memory、queryを外部送信しない。

## v1.0 機能

### プロジェクト

各projectはID、名前、repository path、sanitized Git remote URL、利用AI、言語、作成・更新日時、archive状態、tags、説明、現在の目的を持つ。CLIとUIから登録、一覧、詳細、編集、archive、確認付き削除、project packageのexport/importを行える。

削除には対象IDの完全入力を要求し、削除前に安全化したproject exportを自動生成する。

### 構造化メモリ

対応type:

`decision`, `failure`, `lesson`, `next_action`, `constraint`, `architecture`, `file_reference`, `dependency`, `deployment`, `security`, `note`, `session_summary`

各memoryはID、project ID、type、title、body、importance、tags、作成・更新日時、source、関連files、関連commits、active、verified、import元参照、content hashを持つ。importanceは`high`, `medium`, `low`。

### 登録と移行

- CLI直接入力、UTF-8 stdin、`--input-file`
- ローカルWeb UI
- Markdown、JSON、JSONL
- session終了template
- Git metadataとdiff統計からのrule-based session summary
- 既存RouteCraft Markdown Storeの読み取りimport

AI APIは使用しない。import元は変更せず、同一sourceの同一contentはskip、異なるcontentはconflictとして記録する。

### 検索

- keyword、type、tag、importance、期間、filename、commit、active、verified
- SQLite FTS5が利用可能ならindexを使用し、Unicode部分一致を常にfallbackとして組み合わせる
- title/tag一致、importance、verified、新しさを再rankingへ反映
- 結果にはrelevance、type、importance、登録日、関連filesを含める

完全な日本語形態素解析は対象外。空白区切り、部分一致、tagsの併用を前提にする。

### Context Pack

Markdown、text、JSONに対応する。project概要、現在の目的、重要判断、制約、直近作業、既知問題、失敗、次の作業、重要files、recent commits、受取AIへの指示を含む。

上限:

- Compact: 4,000文字
- Standard: 12,000文字
- Full: 50,000文字
- custom文字数または概算token数

importance、verified、active、新しさを優先し、同一title/bodyの重複を抑える。token数は外部tokenizerなしの概算であり、課金量を保証しない。

### Handoff Pack

folderまたはZIPとして次の6 filesを生成する。

```text
HANDOFF.md
PROJECT_STATE.json
CHANGED_FILES.txt
NEXT_TASKS.md
KNOWN_ISSUES.md
IMPORTANT_DECISIONS.md
```

Git情報はmetadataだけを読み、changed fileの内容を取得しない。秘密情報をmaskし、repositoryの絶対pathは`<REPO_PATH>`へ置換する。

### Git連携

branch、HEAD、remote、working tree、changed/new/deleted files、追加・削除行数、recent commits、tagsを読み取る。subprocessはargument arrayで実行し、commit、branch作成、push、checkout等の書き込みを提供しない。

### ローカルWeb UI

日本語初期表示のSPAを`127.0.0.1`だけで提供する。dashboard、project、memory、search、Context、Handoff、Git、backup/restore、settings、doctorを含む。外部asset、CDN、analytics、fontを読み込まない。

変更APIはJSON、same-origin/Host検証、CSRF tokenを要求する。UIにaccountやremote公開機能はない。

### バックアップ・持ち運び

- DB integrity check後のconsistent SQLite backup ZIP
- archive/manifest/temp DBの検証、現在DBの事前backup、明示`RESTORE`後のatomic restore
- project単位のsafe package
- JSON/JSONL/Markdown export
- DB自体をGitへ自動追加しないsafe JSONL共有
- source ID/content hashによるconflict検出

## CLI入口

主なcommand:

```text
routecraft init
routecraft project add|list|show|edit|rename|archive|delete|backup|restore
routecraft memory add|list|show|edit|delete|search|import|export
routecraft context build
routecraft handoff build
routecraft git status
routecraft session summarize
routecraft loop status|configure
routecraft status
routecraft doctor
routecraft backup
routecraft restore
routecraft export
routecraft import
routecraft ui
```

重要commandは`--help`、human表示、`--json`、意味のある終了codeを持つ。想定利用者errorはtracebackを表示せず、対処可能なmessageを返す。

## 固定条件

- Python 3.11以上の標準libraryだけで動く。
- WindowsとmacOSのpath、日本語、UTF-8 BOM、CRLF、pipe、redirected stdinを扱う。
- telemetryは初期状態で無効。Memory Local本体に外部送信処理を持たせない。
- secret、credential、private key、個人情報候補を保存・export前にmaskする。ただし完全なDLPを保証しない。
- 既存 `routecraft_memory.py`、Decision Store schema v1、既存command contractとの互換性を維持する。既存CLIのversion表示だけは、固定値ではなくinstall済みmanifestを正しく参照するよう修正する。
- Codex RouteCraft Pluginとの連携はopt-inとし、登録済みprojectのbounded Context取得とread-only Git session summary保存だけを自動化する。project作成、semantic decision、next action、Decision Store昇格は自動化しない。

## v1.0対象外

独自cloud同期、共同編集、account、課金、license server、AI API必須機能、常駐監視、IDE専用plugin、LLM同梱、vector DB必須化、remote code execution、AIによる無確認Git書き込み・file削除。

## 既知制約

- Python runtimeはZIPへ同梱しない。
- code signingとmacOS notarizationは行わない。
- 日本語検索は形態素解析ではなくUnicode部分一致中心。
- SQLite DBはOS user権限に依存し、暗号化されない。機微なprojectでは端末disk encryptionを併用する。
- macOS実機検証はrelease前の人間確認項目である。

## 合理的な仮定

- 一人の利用者が同一端末内で操作し、UIをLANへ公開しない。
- repository pathはproject識別とGit metadata取得に必要なためローカルDBへ保存するが、safe export/Handoffではportable placeholderへ置換する。
- Git CLIがない環境でもproject/memory/search/Packは動き、Git表示だけを未取得として返す。
