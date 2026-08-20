# RouteCraft 複数端末フリート運用

この方式は、RouteCraft本体・再利用可能な判断知能・端末固有状態を分離し、Windows、macOS、将来追加する端末を同じ構成で運用するためのものです。

## 基本原則

- **ソースの正本はGitHub**です。ローカルのソースチェックアウトは作業コピーとして扱います。
- **共有可能な設定と判断知能はPrivate GitHub Repository**に保存します。
- **絶対パス、端末ID、Codexの生成キャッシュ、認証情報は各端末だけ**に置きます。
- **全CodexセッションにSource Guardを適用**し、永続的な成果物を変更したタスクだけcommit／push完了を確認します。
- ローカルに未コミット変更がある場合、bootstrapはGitHubの内容で上書きせず停止します。
- 秘密鍵、アクセストークン、個人情報、raw log、会話全文はDecision Storeへ保存しません。

## 標準配置

| 種別 | 全端末の論理パス | 保存先・役割 |
|---|---|---|
| RouteCraftソース | `~/codex-routecraft` | Public GitHubからcloneする作業コピー |
| Decision Store | `~/routecraft-memory` | Private GitHubと同期するCases / Candidates / Rules |
| RouteCraft端末設定 | `~/.codex/routecraft/device.json` | 端末固有の絶対パス、OS、導入バージョン |
| Source Guard設定 | `~/.codex/routecraft/source-control.json` | GitHub owner、Private既定、commit／push方針 |
| Memory CLI設定 | `~/.codex/routecraft/memory.json` | active store、device ID、sync設定 |
| Agent設定 | `~/.codex/agents/routecraft_*.toml` | GitHub上のテンプレートから生成・更新 |
| Plugin cache | `~/.codex/plugins/cache/...` | Codexが生成するローカルキャッシュ |
| 開発リポジトリ | `~/Projects/<repository>` 推奨 | 正本は各GitHub Repository。既存配置は自動移動しない |

Windowsでは`~`が通常`C:\Users\<user>`、macOSでは`/Users/<user>`に展開されます。

## GitHubへ置くもの

### Public Repository

`tamas-hub/codex-routecraft`

- Skill、Agentテンプレート、CLI、bootstrap、テスト、ドキュメント
- すべての端末は原則として`main`をfast-forwardで取得

### Private Repository

RouteCraft Decision Store用のPrivate Repository

- `cases/`
- `candidates/`
- `rules/`
- `templates/`
- `.routecraft-store.json`

共有フリート設定は`.routecraft-store.json`の`fleet`ブロックに保存されます。ここにはGitHub Repository、branch、`~/...`形式の可搬パス、sync方針だけを保存し、端末固有の絶対パスや認証情報は保存しません。

## 各端末にだけ置くもの

- GitHub / Codexの認証状態
- OS固有の資格情報ストア
- `device.json`と`memory.json`
- Codex plugin cache
- Agent設定のバックアップ
- Source Guardのbaseline fingerprint（Git状態のhashだけ。会話本文は含まない）
- cloneした開発リポジトリとbuild成果物

これらはPrivate Decision Storeへcommitしません。

## bootstrapが行うこと

`scripts/bootstrap-device.ps1`または`scripts/bootstrap-device.sh`は、繰り返し実行可能な形で次を行います。

1. RouteCraftソースを`~/codex-routecraft`へcloneまたはfast-forward更新
2. Repository verifierを実行
3. Private Decision Storeを`~/routecraft-memory`へcloneまたは既存接続
4. `auto_sync=both`を設定し、pull/rebaseとpushを実行
5. 共有フリート設定を作成または厳密に照合
6. RouteCraft plugin cacheを安全にバックアップして再導入
7. 6種類のAgent設定を差分確認し、必要時だけバックアップして置換
8. 端末固有の`device.json`を生成
9. source、memory、plugin、Agent、Git状態を最終検証
10. 明示的に有効化した端末では、Source Guardのローカル設定を作成

Private Repositoryが空の場合だけ、最初の端末で`--allow-first-device`を明示します。2台目以降では使用しません。

## Windows

```powershell
Set-ExecutionPolicy -Scope Process Bypass

& "$HOME\codex-routecraft\scripts\bootstrap-device.ps1" `
  -MemoryRemote "https://github.com/OWNER/routecraft-memory-private.git" `
  -EnableProjectSourceGuard `
  -GitHubOwner "OWNER"
```

## macOS

```sh
sh "$HOME/codex-routecraft/scripts/bootstrap-device.sh" \
  --memory-remote "https://github.com/OWNER/routecraft-memory-private.git" \
  --enable-project-source-guard \
  --github-owner "OWNER"
```

## Source Guard

Source Guardは、Git操作をblindに自動実行するHookではありません。Codexへstanding policyを渡し、タスク終了時に今回の作業で生じた未commit／未pushだけを検出します。

- 作業開始時にGit root、HEAD、working tree fingerprintを端末ローカルへ記録
- 作業前から存在するdirty状態はユーザーの作業として保持
- 検証後、Codexがtask-owned filesだけをstageしてcommit／push
- remoteがなければ、機密性と所有権を確認してGitHub owner配下へPrivate Repositoryを作成
- behind／diverged／非GitHub remoteではforceせず停止して報告
- raw Codex transcript、`.env`、資格情報、DB、upload、cache、端末設定は対象外

単なる質問や、永続的なファイルを変更しないセッションにはcommitを作りません。「全セッション対象」とは、すべてのCodexタスクでこの確認を実行するという意味です。

Codexの非managed Hookは端末ごとに一度だけ信頼確認が必要です。bootstrap後のfresh taskで`/hooks`を開き、RouteCraftの`SessionStart`／`Stop`定義を確認して信頼してください。Hookが更新された場合はhashが変わるため再確認されます。

## Codexを使った導入

端末別ZIPの`START_WITH_CODEX.md`をCodexアプリのローカルタスクへ渡すと、Codexが前提条件確認、Git認証、bootstrap実行、最終status確認まで進めます。ユーザー操作が必要なのは、原則としてOSまたはGitHubの認証画面だけです。

## 新しい開発リポジトリ

新規または再取得する製品リポジトリは、原則として次へcloneします。

```text
~/Projects/<repository-name>
```

ソースの正本はGitHubです。3端末間でソースフォルダをOneDriveやiCloud Driveで直接同期しません。各端末はGitHubからclone / pullし、branch / commit / PRで共有します。

既存リポジトリはbootstrapで勝手に移動しません。移動によるIDE設定、署名、build cache、絶対パス参照の破損を避けるためです。整理する場合は、cleanかつpush済みであることを確認し、新しい標準配置へ再cloneする方式を推奨します。

## 障害時の復旧

- RouteCraftソースの異常: `~/codex-routecraft`を退避し、Public Repositoryから再clone
- Decision Storeの異常: 未同期変更を確認後、Private Repositoryから再clone
- Plugin cacheの異常: bootstrapを再実行。既存cacheは日時付きバックアップへ退避
- Agent競合: bootstrapが既存ファイルを日時付きバックアップしてから置換
- Git競合: 自動上書きせず停止。Decision Storeの競合を解消後に再実行

## 端末追加

4台目以降も同じbootstrapを使います。必要なのは、Git、Python 3、Codex、Private Repositoryへのアクセス権だけです。端末追加時に新しい手作業の設定一式を作る必要はありません。
