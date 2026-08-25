# RouteCraft Runtime 0.7.4 スターター

このパッケージは、RouteCraft Local Runtime 0.7.4をWindowsまたはmacOSへ導入するための小さなスターターです。ソースを同梱スクリプトから直接実行するのではなく、公式Repositoryからrelease tagを取得し、固定commitと一致した場合だけRepository verifierと公式setup scriptを実行します。

## Release pin

- Repository: `@ROUTECRAFT_REPOSITORY@`
- Tag: `@ROUTECRAFT_TAG@`
- Commit: `@ROUTECRAFT_COMMIT@`
- Codex CLI: `@ROUTECRAFT_CODEX_CLI_VERSION@`（この版だけを受け付けます）

実行前に、ZIPと同時に配布された`SHA256SUMS.txt`でSHA-256を照合してください。同じ配布経路から得たchecksumは破損や取り違えを検出しますが、未署名パッケージの発行者真正性まで保証するものではありません。

## 前提条件

- Git
- Python 3.11以上
- Codex CLI `@ROUTECRAFT_CODEX_CLI_VERSION@`（`codex --version`が`codex-cli @ROUTECRAFT_CODEX_CLI_VERSION@`と完全一致）
- 公式Repositoryを取得できるネットワーク
- 各端末で完了済みのCodex認証

Python、Codex CLI、認証情報はこのZIPに含まれません。管理者権限を要求せず、認証値を入力・コピー・表示しません。

このstarterによる初回導入には、固定tagを公式Repositoryから取得するネットワーク接続が必要です。導入後のLocal Runtimeはoffline-firstで、Control Centerは別製品かつoptionalです。Control Centerの未設定、停止、未契約、通信障害を理由にLocal Runtimeを停止しません。

## Windows

展開前にPowerShellでhashを表示し、`SHA256SUMS.txt`の同名行と完全一致することを確認します。

```powershell
Get-FileHash .\routecraft-runtime-0.7.4-windows.zip -Algorithm SHA256
Get-Content .\SHA256SUMS.txt
```

Windowsがダウンロード由来のMark-of-the-Webを付けた場合は、hash照合とスクリプト内容の確認後に、展開先のスクリプトだけを明示的に解除します。ディレクトリ全体は一括解除しません。

```powershell
Unblock-File .\routecraft-runtime-0.7.4-windows\install-routecraft.ps1
```

最初にPlanを確認します。Planはclone、fetch、checkout、Plugin変更を行いません。

```powershell
.\install-routecraft.ps1 -Mode Plan
```

内容を確認後、固定文字列`INSTALL`を明示して適用します。

```powershell
.\install-routecraft.ps1 -Mode Apply -Confirm INSTALL
```

既定のソース配置は`$HOME\codex-routecraft`です。別の場所を使う場合は`-SourceDir`を明示します。

## macOS

展開前にhashを照合します。

```sh
shasum -a 256 routecraft-runtime-0.7.4-macos.zip
cat SHA256SUMS.txt
```

```sh
./install-routecraft.sh --plan
./install-routecraft.sh --apply --confirm INSTALL
```

既定のソース配置は`$HOME/codex-routecraft`です。別の場所を使う場合は`--source-dir`を明示します。

## 含まれないもの

- RouteCraft Control Center Add-on
- Private Decision StoreのURL、内容、認証
- Memory LocalのDBまたはbackup（Memory Local 1.0.0は別製品として変更しません）
- Graph State、checkpoint、benchmark結果
- Codexの認証ファイル、Sandbox credential、Plugin cache
- 端末固有の絶対パス

Private Decision Storeは、Runtime導入とfresh Codex taskでの確認が終わってから、所有者が管理する既存の手順で端末ごとに接続してください。このスターターはURLを尋ねず、自動接続もしません。

適用は`routecraft-device install`のtransactionとして記録されます。JSON出力の`transaction_id`を保管してください。既存のclean checkoutを使った導入が途中で失敗した場合、installerは導入前のbranchまたはdetached HEADとcommitへ戻し、commit一致まで検証します。導入成功時は検証済みrelease commitのdetached checkoutを維持します。

Plugin等のtransactionを明示的に戻す場合は、固定checkout内でplatformに応じて次を実行します。

```powershell
python .\plugins\codex-routecraft\scripts\routecraft_device.py rollback --source-dir <checkout> --transaction-id <install-id> --confirm ROLLBACK --json
```

```sh
python3 ./plugins/codex-routecraft/scripts/routecraft_device.py rollback --source-dir <checkout> --transaction-id <install-id> --confirm ROLLBACK --json
```

RollbackはRouteCraftが所有するPlugin registration、marketplace、cache、6 Agents、local configだけを対象とし、Memory Local、Decision Store、Control Centerには触れません。

## 完了後

開いているCodex taskを閉じ、fresh taskを開始してください。Plugin、6 Agents、Hooks、Graph Mode、Doctorの確認は、導入した固定commitのドキュメントに従って行います。

Windows用スクリプトはAuthenticode署名されておらず、macOS用スクリプトも署名・notarizeされたアプリではありません。テキスト内容、release pin、SHA-256を確認してから実行してください。MIT Licenseは各starterとsource archiveへ同梱されます。
