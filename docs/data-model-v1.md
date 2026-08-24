# RouteCraft Memory Local v1.0 データモデル

## version管理

- product version: `1.0.0`
- SQLite schema: `PRAGMA user_version = 1`
- export/package manifest: `schema_version = 1`
- unknown newer schemaは読み書きを拒否する。

SQLite DBと既存RouteCraft Markdown Storeのschema versionは独立している。既存Storeは変更せずimport元として扱う。

## projects

| field | type | 条件 |
|---|---|---|
| id | TEXT | primary key、生成ID |
| name | TEXT | 必須、空白のみ不可 |
| repo_path | TEXT | 任意、local absolute path |
| git_remote_url | TEXT | 任意、userinfo/tokenを除去 |
| ai_agents | JSON TEXT | string list |
| languages | JSON TEXT | string list |
| created_at | TEXT | UTC ISO-8601 |
| updated_at | TEXT | UTC ISO-8601 |
| archived | INTEGER | 0/1 |
| tags | JSON TEXT | string list |
| description | TEXT | sanitized |
| current_objective | TEXT | sanitized |

project deleteはmemoryをcascade削除するため、ID完全確認と事前exportを必須にする。

## memories

| field | type | 条件 |
|---|---|---|
| id | TEXT | primary key、import時は安全な既存IDも保持可能 |
| project_id | TEXT | projects FK、cascade delete |
| type | TEXT | 指定された12 type |
| title | TEXT | 必須、sanitized |
| body | TEXT | 必須、sanitized |
| importance | TEXT | high/medium/low |
| tags | JSON TEXT | string list |
| created_at | TEXT | UTC ISO-8601 |
| updated_at | TEXT | UTC ISO-8601 |
| source | TEXT | cli/ui/import/git-rule-based等 |
| related_files | JSON TEXT | sanitized relative/portable references推奨 |
| related_commits | JSON TEXT | commit hash/reference |
| active | INTEGER | 0/1、既定1 |
| verified | INTEGER | 0/1、既定0 |
| source_ref | TEXT | legacy ID、file/package ID |
| content_hash | TEXT | normalized安全化contentのSHA-256 |
| legacy_metadata | JSON TEXT | secret除去済み互換metadata |

同一project内で`source_ref`が一致するimportはcontent hashを比較する。同一ならskip、異なる場合はconflictとして既存memoryを保持する。

`loop_session_summaries`は`(project_id, source_ref)`をprimary keyにした追加tableである。Loop Stopの`session_summary`だけはここで一意性を保証し、並行した同一sessionの再実行でも1件へ収束する。既存の一般memoryにある重複`source_ref`は削除・上書きせず、最古のrecordを既存結果として採用する。

## import_conflicts

| field | 説明 |
|---|---|
| id | 自動採番 |
| project_id | 対象project |
| source_ref | 衝突したsource identity |
| existing_hash | 現在content hash |
| incoming_hash | 取り込み候補hash |
| detected_at | UTC ISO-8601 |
| details | secretを含まないJSON metadata |
| resolved | 0/1 |

v1.0はconflictを検出・保持し、UI/CLIへ件数を返す。自動mergeは行わない。

project packageのimportはmanifestとpayloadの双方で`schema_version = 1`を必須にし、project、memory、FTS派生index、conflict rowを一つのSQLite transactionで書き込む。新規projectとして復元する場合はarchived/active/verifiedとtimezone付きcreated_at/updated_atを検証して保持する。事前validationまたは後段writeが失敗した場合、batch途中のrecordは残さない。

## settings

key/value JSONのlocal table。初期値:

- `language`: `ja`
- `telemetry_enabled`: `false`（v1.0本体に送信処理なし）
- `excluded_globs`, `excluded_directories`, `excluded_extensions`
- UI/Packの安全な既定値

secretそのものをsettingsへ保存しない。

## search index

FTS5が有効なSQLiteではtitle、body、tags、related filesをindexする。FTS indexはDBから再構築できる派生dataであり、正本ではない。FTS5 unavailable時または日本語phraseではUnicode `LIKE`/substringとPython scoreを使用する。

## export形式

### JSON/JSONL

UTF-8、schema version付き。project packageはproject metadataとmemoriesを含む。safe modeではrepo absolute pathをplaceholder化し、secret scannerを再適用する。

### Markdown

人間が読めるheadingとmetadataを出力する。再importではheadingまたはfile名をsource referenceとして扱う。

### project package

- manifest JSON
- portable project JSON
- memories JSONL
- 任意の説明Markdown

ZIP entryはrelative pathだけ。SQLite DB、`.env`、key/cert、cacheを含めない。

## 既存RouteCraft Store mapping

| legacy kind | v1 type | verified | legacy ID |
|---|---|---|---|
| rule | decision | true（validated時） | source_refへ保持 |
| case | lesson、失敗sectionが中心ならfailure | true | source_refへ保持 |
| candidate | note | false | source_refへ保持 |

frontmatterのtags、evidence、scope等は安全化してtags、related references、legacy metadataへ保持する。元Markdownは変更しない。

## 時刻と文字code

- durable timestampsはUTC、timezone付きISO-8601。
- textはUnicode。file I/OはUTF-8、inputはUTF-8 BOMを許可。
- export改行はLF。CRLF inputは正常化して受理する。
