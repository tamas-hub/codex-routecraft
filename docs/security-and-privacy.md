# RouteCraft Memory Local セキュリティとプライバシー

## 保証する境界

- 基本機能は端末内だけで動き、Memory Local本体はHTTP client、AI API、analytics、telemetry送信を実行しない。
- Web UIは`127.0.0.1`だけへbindする。
- Gitはread-only commandだけを使い、file内容、credential helper、commit/pushを扱わない。
- DBをGitへ自動追加しない。
- import、保存、safe export、Handoffでsecret patternをscan/maskする。

RouteCraft Memory Localはsandbox、anti-malware、完全なDLP、disk encryptionではない。patternに一致しない機微情報は利用者が保存前に除外する必要がある。

## 初期状態で保存しないもの

- `.env`の値、password、API key、access/refresh/OAuth token
- Cookie、Authorization/Bearer header
- SSH/private key、certificate private key
- GitHub/OpenAI/AWS/JWT等のcredential pattern
- email address、電話番号等の個人情報候補
- 除外directory/file extension/globに該当するfile

検出時はvalueを`[REDACTED:<kind>]`へ置換し、warningにはkindだけを載せる。元のvalueをexception、HTTP log、CLI logへ出さない。

## 既定除外

`.env*`, private key/cert files、`.git`, `node_modules`, virtual environment、cache、build/dist directory等。利用者はsettingsでglob、directory、extensionを追加できる。

Git session summaryはchanged filenameとline統計だけを読み、file本文を取り込まない。このため`.env`の差分内容がmemoryへ入ることはないが、秘密を含むfilename自体はmask対象になり得る。

## RouteCraft Loop bridge

- bridgeは明示的に有効化した端末でだけ動き、未登録repositoryを自動登録しない。
- SessionStartは登録済みprojectのbounded ContextだけをCodexへ渡す。
- Stopはraw transcriptやassistant responseを読まず、read-only Git metadataから未確認の`session_summary`だけを保存する。
- session IDはDBへ保存せず、一方向hashを`source_ref`の重複防止に使う。
- 同じhashのStopが並行してもSQLite transactionとdurable keyで1件だけを保存する。既存recordを削除・上書きして重複を解消することはしない。
- bridge設定とsession sidecarは`~/.codex/routecraft/`配下の端末local dataであり、Decision Storeへ同期しない。
- Memory Local data directoryまたはそのancestorにDecision Store sentinelがある場合はfail closedとし、両Storeの同一directory利用とDecision Store配下への作成を拒否する。
- Source Guardはbridgeを遅延読込し、package不存在またはOFF時に既存Loopの判定を変えない。bridge内部の失敗はHook process全体をcrashさせず、local warningとして返す。
- 合成Contextは6,000文字のHook上限内に切り詰め、Source Guardとevaluation policyをproject memoryより先に保持する。
- Hookの標準入出力はWindows code pageに依存せずUTF-8へ固定する。

## Web threat model

local browserからの同一利用者操作を想定する。LAN共有、multi-user authentication、reverse proxy公開は対象外。

対策:

- bind addressを`127.0.0.1`へ固定
- Host header allowlistでDNS rebindingを抑止
- mutationにJSON content type、same-origin、session CSRF tokenを要求
- permissive CORSを出さない
- 1 MiBのrequest body上限
- CSP、frame deny、nosniff、API no-store
- UIへ差し込むproject/memory値は共通escape関数を通し、API結果を未escapeのHTMLとして扱わない
- request body、query、CSRF tokenをaccess logへ出さない

UIをproxy経由で公開したり、firewall越しに共有してはならない。

## DBとbackup

- SQLite DBはOS user権限で保護されるが暗号化されない。
- laptop紛失等に備える場合はBitLocker/FileVault等のdisk encryptionを利用する。
- backup ZIPも暗号化されないため、機微projectの持ち運びでは暗号化されたmedia/containerを使う。
- restore前にarchive path、manifest、temp DB integrity、schema/index初期化を候補DB上で検証し、current DBのpre-restore backupとraw rollback copyを作る。live DBは検証済み候補との単一atomic replaceで常にcanonical pathへ残し、置換後の初期化が予期せず失敗した場合だけraw copyを戻す。自動復帰にも失敗した場合はZIPとraw copyを保持して復旧先を表示する。復元成功後にraw copyのcleanupだけが失敗した場合は成功を維持し、CLI/Web UIまでwarningと保持pathを返す。失敗時に候補tempも削除できなければ元の失敗理由と残置pathを併記する。
- restoreは内部のUI／CLI／Stop hook writerと共通のprocess間operation lockを、pre-restore backup作成からDB置換完了まで保持する。
- import/project packageは展開後sizeを制限し、backup DBは上限付きstreamとしてtemp fileへ検証する。ZIPを一括でmemoryへ展開しない。
- project packageのproject IDはpath separatorやdrive指定を含まないsafe identifierだけを受理する。project削除前のsafety copyはID非依存のrandom file名を使い、解決後の親directoryがMemory Local data directoryと一致することを確認し、全件exportからcascade削除まで共通operation lockを保持する。
- project/memory IDにもsecret scannerを適用し、新規importではsecret形式を拒否する。旧DBに残るunsafe IDはsafe export/package作成時に新しいopaque IDへ置換し、元IDを出力しない。
- secret/pathを含む`source_ref`はraw値や共通redaction placeholderをidentityに使わず、一方向hashのopaque参照へ変換する。これにより異なる参照の衝突を避け、再import時の同一性を保つ。
- metadataはvalueだけでなくkeyにもsecret scannerを適用し、unsafe keyを一方向hashのopaque keyへ置換する。
- portable exportはdrive/POSIX pathに加えてUNC、Windows root-relative、extended-length path、`file://` URIも、値全体と文章中の双方で`<PATH>`またはopaque source referenceへ変換する。`https://`等のnetwork URLは保持する。

## 持ち運び

safe project packageはrepo absolute path、secret pattern、DBを除外する。Gitへ置ける形式だが、project名、内部file名、設計判断そのものが機微情報であり得る。公開repositoryへ置く前に内容を人間が確認する。
Handoff Packにもsafe exportと同じ絶対／UNC／root-relative／`file://` path maskingを適用する。

## supply chain

v1.0 ZIPは第三者package、JavaScript、font、runtimeを同梱しない。SHA256でdownload/transfer後の一致を確認できる。code signing/notarizationは未実施であり、配布者本人が公開前にhashとarchive内容を確認する。

## incident時

1. UIを`Ctrl+C`で停止する。
2. 該当memoryをactive=falseへし、必要ならID確認付きで削除する。
3. backup/exportにも残っている可能性を確認する。
4. credentialを保存した可能性がある場合は、mask済みかに関係なくcredentialを失効・再発行する。
5. `routecraft doctor`とDB integrity checkを実行する。

## privacy上の未保証

- 自由文から全ての個人情報・社内固有識別子を検出すること
- 他のlocal user/admin/malwareからDBを隔離すること
- browser extensionが表示内容を読むことの防止
- safe exportを公開してよいかの組織policy判断
- Codexへ注入したContextが、利用中のCodex product側でどのように保持されるか
