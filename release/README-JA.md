# RouteCraft Memory Local v1.0 クイックスタート

RouteCraft Memory Localは、開発プロジェクトごとの判断、失敗、制約、次の作業を端末内だけへ保存し、次のAIへ渡すContext Pack／Handoff Packを作るツールです。アカウント、有料API、Docker、クラウド接続は不要です。

## 必要なもの

- Python 3.11以上
- Git 2.x（Git状態を表示する場合だけ）

PythonはこのZIPに同梱していません。公式配布元またはOSのpackage managerから導入してください。

## 初回起動

### Windows

PowerShellで展開先へ移動し、次を実行します。

```powershell
.\routecraft.ps1 init
.\routecraft.ps1 project add --name "サンプル開発" --repo "C:\path\to\repository"
.\routecraft.ps1 ui
```

実行policyでPowerShell scriptを起動できない場合は、policyを恒久変更せず次を使えます。

```powershell
py -3 .\app\routecraft.py --help
```

### macOS

Terminalで展開先へ移動し、次を実行します。

```sh
chmod +x ./routecraft
./routecraft init
./routecraft project add --name "サンプル開発" --repo "/path/to/repository"
./routecraft ui
```

未署名のshell scriptのため、macOSが初回確認を表示する場合があります。内容はテキストとして確認でき、ネットワーク公開や管理者権限を必要としません。配布元と同梱SHA256を確認してから実行してください。

## デモデータ

最初にprojectを登録し、表示されたIDを指定します。

```text
routecraft memory import --project <PROJECT_ID> --input samples/demo-memories.jsonl --format jsonl
routecraft memory search --project <PROJECT_ID> "日本語 stdin"
routecraft context build --project <PROJECT_ID> --profile compact
routecraft handoff build --project <PROJECT_ID> --output handoff --zip
```

## 保存場所

既定では `~/.routecraft-memory-local/` にDB、backup、exportを保存します。SQLite DBをGitへ自動追加しません。テレメトリと外部送信はありません。

別の保存場所を使う場合は、全コマンドで `--data-dir <PATH>` をsubcommandより前へ指定します。

## 安全な復元

```text
routecraft backup --output my-backup.zip
routecraft restore --input my-backup.zip --confirm RESTORE
```

復元はarchiveとDBのintegrityを確認し、現在DBの事前backupを作ってから置換します。

詳しい仕様、制約、セキュリティ説明は `docs/` を参照してください。

## RouteCraft Loopと連携する場合

Codex RouteCraft Pluginも導入済みなら、登録済みprojectのContextをSessionStartで自動取得し、正常なStop時にGit metadataだけのsession summaryを保存できます。

```powershell
.\routecraft.ps1 --data-dir "$HOME\.routecraft-memory-local" loop configure --enable --context-profile compact
.\routecraft.ps1 loop status
```

設定は`~/.codex/routecraft/local-memory.json`へ保存され、既存設定がある場合はtimestamp付きbackupを作ります。反映には新しいCodexタスクが必要です。停止するときはdataを削除せず次を実行します。

```powershell
.\routecraft.ps1 loop configure --disable
```

bridgeはprojectを自動作成せず、raw transcriptやfile本文を読みません。設計判断と次の作業は検証後にCLI/UIから明示保存してください。`~/.routecraft-memory-local`を既存`routecraft-memory` Decision Storeと同じdirectoryへ設定することはできません。

Memory evaluatorのround-robin実験中は`off`試行を汚さないためbridge全体を停止します。通常運用で自動Context／summaryを使う場合は、evaluatorを実験なしの`full` modeへ設定してください。`recall` modeではContextだけを使い、Stop保存は行いません。
