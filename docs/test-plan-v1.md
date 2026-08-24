# RouteCraft Memory Local v1.0 テスト計画

## 原則

- current repository evidenceと再現可能なtestを正とする。
- 既存RouteCraft suiteを全て維持する。
- test dataはtemporary directoryとtemporary Git repositoryだけを使い、個人Decision Storeや外部serviceへ触れない。
- Windowsでは`python -X utf8`またはprocess-local `PYTHONUTF8=1`を使い、設定を永続変更しない。

## test matrix

| 領域 | 主な確認 |
|---|---|
| unit | ID、timestamp、validation、token概算、ranking、dedup |
| DB | schema、FK、transaction、FTS/fallback、empty data、pagination |
| migration | old user_version、pre-migration backup、unknown newer拒否、再実行性 |
| project | add/list/show/edit/archive、ID確認delete、安全copy |
| memory | 12 types、importance、active/verified、CRUD、filters |
| import | Markdown/JSON/JSONL/BOM/CRLF、legacy Store、重複skip、conflict、project package全体rollback |
| CLI | help、human/JSON、exit code、stdin/pipe/input-file、不正input |
| search | 日本語部分一致、空白語、tag、importance/recency、inactive除外 |
| Loop bridge | 明示enable/disable、登録済みprojectだけのContext、並行Git summary重複防止、raw session ID非保存、Decision Store directory拒否、package不存在時互換、6,000文字合成上限、Windows UTF-8 pipe |
| Context | 3 formats、3 profiles、custom cap、section、token概算、dedup |
| Handoff | required six files、folder/ZIP、secret/path除去 |
| Git | non-repo/clean/dirty、Japanese filename、remote credential除去、writeなし |
| security | token/key/env/auth/cookie/email/phone、warning非漏洩、exclusion |
| backup | integrity、ZIP path、restore confirm、prebackup、破損時無変更 |
| UI/API | loopback、Host/Origin/CSRF/content-type、全主要screen/API、error envelope |
| responsive | 375px/desktop、日本語長文、overflow、focus、keyboard、console |
| volume | 1,000 memory投入、list/search/Contextの実用速度 |
| release | deterministic ZIP、entry allowlist、SHA256、clean extract smoke |

## 既存baseline

実装前の2026-08-23時点:

- `python scripts/verify.py`: success
- `python -m unittest discover -s tests -v`（UTF-8環境）: 51 tests、50 pass、1 Windows symlink skip
- 日本語redirected stdin回帰: pass

## 標準実行

```powershell
$env:PYTHONUTF8='1'
$env:PYTHONIOENCODING='utf-8'
python scripts/verify.py
python -X utf8 -m unittest discover -s tests -v
python scripts/evaluate_routecraft_local.py
python scripts/build_local_release.py --output-dir dist
```

PowerShell/sh syntaxも確認する。外部networkやreal remoteへのpushはtestしない。

## 1,000件performance

temporary DBへ12 type、3 importance、日本語tagsを分散した1,000件を投入する。少なくとも次を計測する。

- batch/連続登録の総時間
- project memory listの時間
- keyword+type+tag検索の時間
- Standard Context生成の時間

絶対的benchmarkを製品保証にはしないが、通常のdeveloper PC上で各interactive operationが数秒以内で終わることをgateにする。CI負荷差を考慮して上限には余裕を持たせ、実測値を最終報告へ載せる。

## UI visual QA

1. temp data dirでUIを127.0.0.1の空きportへ起動。
2. desktop代表幅と375pxでdashboard、project、memory、search、Contextを表示。
3. 長い日本語project名、長path、empty/error/loading、delete confirmationを確認。
4. horizontal overflow、clip、overlap、focus visibility、tab order、tap targetを確認。
5. console errorとfailed local requestsを確認。

## macOS確認範囲

Windows上でpathlib/subprocess shell=False、LF、shell syntax、ZIP permission、case-sensitive archive pathを確認する。ただしmacOS実機でのlauncher、Gatekeeper、Python discovery、Japanese pathは未検証としてrelease gateに残す。

## 合格条件

- 既存suiteと新規suiteにfailureがない。
- skipはplatform制約が説明され、critical pathを隠さない。
- retrieval評価の期待memoryがtop Kへ入り、inactiveを除外し、Context重複がない。
- backup破損・secret・cross-origin・confirm不足のnegative testが現在dataを変更しない。
- batch importの後半validation/runtime failureが先行rowを残さずrollbackする。
- project package importのproject、memory、FTS、conflict rowが後半failure時に一括rollbackする。
- path-likeなproject IDを含むpackageを拒否し、project削除前のsafety copyがdata directory直下から出ない。
- secret形式のmemory IDをimport時に拒否し、旧DB由来のunsafe IDをsafe export/packageへ出さない。
- GitHub fine-grained PATを本文・ID・safe exportで検出し、raw値を残さない。
- active/verified/archivedはJSON booleanまたは0/1だけを受理し、文字列`"false"`をtrueへcoerceしない。
- 複数のsecret形式legacy source referenceを相互に衝突させず、再import、safe JSONL、project packageで同一性を保つ。
- metadata keyのcredentialと、値全体または文章中のUNC／root-relative／extended-length Windows／POSIX／`file://` pathをsafe export/packageへ残さず、network URLは保持する。
- project削除のsafety packageはUIのlist上限を使わず全memoryを含み、restore中の内部writerはpre-restore backupとDB置換の後まで待機する。
- restore候補でschema/index初期化をpreflightし、単一atomic replace後の予期しない初期化failureでは旧DBを復帰する。自動復帰failure時はZIP/raw recovery artifactの双方を検証して保持し、成功後のraw cleanup failureはCLI/Webまで成功+warningとして伝える。候補temp cleanup failureは元エラーと残置pathを保持する。Handoff Packにもsafe exportと同じpath maskingを適用する。
- memory searchは省略時activeのみ、`active=false`でinactiveのみ、`active any`/API `null`で双方を返す。
- project packageのarchive/active/verifiedとtimezone付きcreated_at/updated_atを検証して復元する。
- 既存projectへのpackage mergeはmemory IDだけでなく`source_ref` identityを照合し、同一内容をskip、異なる内容をconflictとして保持する。
- Decision Store Caseの`Failed approaches`見出しだけでfailureへ誤分類せず、Caseをverifiedとして取り込む。
- Git rename/copyはporcelainのtarget/source順を守り、changed fileに新path、previous pathに旧pathを返す。
- 同一sessionのStopを並行実行してもdurable keyが1件のsummaryへ収束する。
- bridgeがOFFまたは未同梱でもDB／sidecarを作らず、既存Hook結果を変えない。
- Windowsの非UTF-8 code page環境でもHook stdin/stdoutの日本語ContextがUTF-8で往復する。
- release ZIPをclean temp展開して主要CLI flowが成功する。
