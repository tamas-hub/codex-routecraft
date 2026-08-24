# RouteCraft Memory Local v1.0 リリース計画

## 配布形態

- `routecraft-memory-local-1.0.0-windows.zip`
- `routecraft-memory-local-1.0.0-macos.zip`
- `SHA256SUMS.txt`
- `release-manifest.json`

どちらもPython 3.11以上を前提とし、runtimeや第三者packageを同梱しない。WindowsはCMD/PowerShell launcher、macOSはPOSIX shell launcherを含む。

## 生成

```powershell
$env:PYTHONUTF8='1'
python scripts/build_local_release.py --output-dir dist
```

builderはsorted entry、固定timestamp、固定permissionでZIPを作り、path traversal、DB、`.env`、key/certの混入を拒否し、ZIP CRCとSHA256を検証する。

## release gate

1. working treeの意図したdiffだけを確認する。
2. `python scripts/verify.py`。
3. `python -X utf8 -m unittest discover -s tests -v`。
4. retrieval評価を実行し、Hit@K/MRR、inactive除外、Context coverage、duplicateを確認する。
5. UIを実起動し、375px/desktop、主要操作、console/network errorを確認する。
6. Windows ZIPをclean temp directoryへ展開し、`--version`, `--help`, init, project add, demo import, search, Context, Handoff, backup/restoreをsmoke testする。
7. macOS ZIPのentry permission、shell syntax、path処理をWindows上で確認し、macOS実機でも同じsmoke testを行う。
8. SHA256SUMSを再計算し、manifestと一致させる。
9. archive内にDB、credential、absolute local path、cacheがないことをscanする。
10. fresh reviewerの`ship` verdictを得る。修正後はreviewを取り直す。

## version方針

Memory Local productは`release/VERSION`と`routecraft_local.VERSION`を一致させる。既存Codex plugin versionは別互換面であり、本作業では0.5.1系を不用意に1.0へ変更しない。

schema変更はSemVerとSQLite `user_version`の双方を更新し、旧schema fixture、pre-migration backup、forward migration、rollback手順を必須にする。

## code signing/notarization

v1.0はcode signingとApple notarizationを行わない。実行scriptはtextとして確認でき、管理者権限や常駐登録を要求しない。配布pageには次を明記する。

- Python runtimeを同梱しないこと
- 未署名scriptであること
- SHA256確認方法
- localhostだけで動くこと
- DB/backupが暗号化されないこと

## rollback

- source release: 以前のZIPを再配布できるようartifact/hashを保存する。
- data: 新version起動前backupとrestore commandを使う。未知の新schemaを旧versionで開かない。
- plugin: 既存 `routecraft_memory.py`は独立しており、Memory Localを削除してもDecision Store dataを変更しない。

## BOOTH配布前の人間確認

- 商品名、価格、license、support範囲、連絡先、更新提供方法
- Windows 11とmacOS現行versionの実機smoke test
- SmartScreen/Gatekeeperの実際の表示と説明文の一致
- ZIP名、VERSION、CHANGELOG、SHA256、archive content
- sampleに個人情報、private repo名、absolute path、credentialがないこと
- READMEだけで新規利用者が起動できること
- backup/restoreとuninstallでdataの扱いが誤解されないこと
- macOS launcherの実行permissionが保持されること
- 既知制約と「完全なsecret検出ではない」説明

## 将来version候補

- 明示BYOKのAI summary adapter
- macOS/Windowsのsigned standalone executable
- conflict解決UIと差分preview
- optional encryption at rest
- English UI
- IDEからの明示handoff起動（常駐監視なし）
