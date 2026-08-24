# RouteCraft Memory Local v1.0 アーキテクチャ

## 設計方針

既存RouteCraft pluginに追加するが、既存Markdown Decision Storeを置換しない。新製品は別entry point、別package、別SQLite DBを持ち、legacy Storeを明示importできる。

```text
CLI routecraft.py ─┐
                   ├─ RouteCraftService ─ Database/FTS5
Web UI / JSON API ─┘         │             ├─ projects
                             │             ├─ memories
                             │             └─ conflicts/settings
                             ├─ Security sanitizer/exclusion policy
                             ├─ Import/export/backup/restore
                             ├─ Git read-only inspector
                             └─ Context/Handoff builders

RouteCraft SessionStart/Stop hook ─ opt-in loop_bridge ─┘

Legacy Markdown Store ── explicit read-only import ──┘
```

## 配置

```text
plugins/codex-routecraft/scripts/
  routecraft.py
  routecraft.ps1
  routecraft.sh
  routecraft_local/
    __init__.py       versionとenum
    errors.py         安定したerror/exit分類
    core.py           SQLite接続、schema、migration
    security.py       検出、mask、除外
    service.py        project/memory/import/export/backup
    git_tools.py      Git metadata読み取り
    packs.py          Context/Handoff
    loop_bridge.py    opt-in SessionStart/Stop連携
    cli.py            argparseと表示
    ui.py             loopback HTTP/JSON API
    web/              dependencyなしSPA
```

## データフロー

### 保存

1. CLI/UIが入力をserviceへ渡す。
2. serviceが型、長さ、ID、project存在を検証する。
3. security層がtitle/body/metadataをscanし、該当valueをmaskする。
4. transaction内でrowとFTS indexを更新する。
5. masked findingのkindだけを利用者へ返し、元の値はlogへ出さない。

### Recall

1. projectとfiltersでcandidate rowを絞る。
2. FTS5が利用できるqueryではindexを使い、日本語を含む全queryでUnicode部分一致をfallbackとする。
3. title/tag/keyword、importance、verified、recencyをserviceでscoreする。
4. active=falseは明示指定がない限り除外する。

### Context Pack

1. projectとactive memoriesを取得する。
2. importance、verified、更新日時で優先順位を決める。
3. normalized title/bodyで重複を除く。
4. semantic sectionへ分類し、profile/custom上限へ収める。
5. char countとoffline token概算を付ける。

### Handoff Pack

1. Context相当の重要情報とGit metadataを取得する。
2. required six filesをmemory上で構築する。
3. 全artifactを再scan/maskし、絶対repo pathをplaceholder化する。
4. folderまたはZIPへ書き、ZIP entryをrelative pathへ限定する。

### RouteCraft Loop連携

1. `routecraft loop configure --enable`で端末localの専用設定を作る。初期状態は未設定・OFFである。
2. SessionStartでcurrent Git rootと完全一致する登録済みprojectだけを選ぶ。projectは自動作成しない。
3. Compact Context Packを最大5,000文字以内でhook contextへ追加し、既存Source Guard／evaluation contextとの合計を6,000文字以内へ収める。超過時は優先度の低い末尾のproject memoryを切り詰め、既存policyを保持する。これはprior evidenceであり、current sourceより優先しない。
4. 正常にStopできる段階で、session開始時からGit状態が変化していればfilename、line統計、commit metadataだけの`session_summary`を保存する。
5. raw transcript、prompt、file本文、credentialは読まない。設計判断と`next_action`はAIまたは利用者が検証後に明示保存する。
6. Decision Storeは汎用Case/Rule、Memory Localはproject作業記憶という責務を維持し、自動双方向同期しない。
7. evaluatorのround-robin experimentまたは`mode=off`ではbridgeをDB open前に停止する。`mode=recall`ではContextだけを許可し、Stop保存を行わない。
8. Source Guardはbridgeを遅延importする。Local packageが未同梱、無効、または利用不能でも既存Loopを停止せず、従来のSource Guard／evaluation結果を維持する。
9. Hook process自身がstdinをUTF-8 BOM対応、stdout/stderrをUTF-8へ固定し、Windowsのlegacy code pageに依存しない。
10. Stop summaryは`loop_session_summaries`のdurable keyと短い`BEGIN IMMEDIATE` transactionで同時実行を1件へ収束させる。既存の一般memoryにある重複`source_ref`は削除しない。

## ローカルWeb境界

- server bindは`127.0.0.1`固定。別host指定は起動前に拒否する。
- Host headerはlocalhost/127.0.0.1の実portだけを許可する。
- mutationは`application/json`、same-origin、bootstrap発行CSRF tokenを要求する。
- CSPは`default-src 'self'`を基本に外部connect/font/image/scriptを許可しない。
- API responseは`no-store`、frameはdeny、MIME sniffingを禁止する。
- access logへrequest body、query、tokenを記録しない。

## migrationと復旧

- DB schemaは`PRAGMA user_version`で管理する。
- 新規DBはtransactionでschema v1を作る。
- 既存の古いDBにtableがある場合、migration前にSQLite backup APIでconsistent copyを作る。
- 現行より新しいschemaはfail closedとし、未知schemaへ書かない。
- restoreはZIP path検査、manifest、temp DB integrity check、pre-restore backup、atomic replaceの順で行う。
- import conflictは既存rowを上書きせず`import_conflicts`へ残す。
- JSON/JSONL batch importは全recordを事前検証し、memory row、FTS row、conflict rowを1 transactionで確定する。

## failure pointsと扱い

| failure | 扱い |
|---|---|
| SQLite lock | busy timeout後に具体error。transactionをrollback |
| FTS5なし | LIKEとPython rankingへfallback |
| Gitなし/non-repo | product本体は継続し、Git項目を未取得として返す |
| 不正/BOM/CRLF input | UTF-8-sigでdecode、schema errorはrecord単位で報告 |
| secret検出 | valueをmaskし、kindだけwarning |
| import同一sourceが変化 | conflict記録、blind overwriteしない |
| backup破損 | current DBを変更せずrestore拒否 |
| UIへのcross-origin mutation | Host/Origin/CSRF/content-typeで拒否 |

## 拡張点

- `memory type`はschema enumをversioned migrationで拡張できる。
- 高度なAI summaryは将来の明示BYOK adapterとしてservice外へ置く。
- 英語UIは既存の文言辞書を追加する。
- cloud同期はDB同期ではなくsafe packageの明示transportを基本とする。

## 非依存性

runtime dependencyはPython 3.11以上だけ。Git integration使用時のみGit CLIを呼ぶ。Node、browser framework、ORM、web framework、FTS extension download、Docker、network accessは不要。
