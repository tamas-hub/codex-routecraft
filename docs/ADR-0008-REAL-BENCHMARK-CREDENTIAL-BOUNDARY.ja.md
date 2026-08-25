# ADR-0008: Real Benchmark の認証境界と platform capability

- Status: Accepted
- Date: 2026-08-25
- Runtime target: 0.7.3
- Related: ADR-0007, Graph IR v1, transport v4

## Context

RouteCraft 0.7.0 の Real Agent Benchmark は、一時 `CODEX_HOME` に通常の
`auth.json` を複製し、その home で `codex sandbox` と `codex exec` を起動する設計だった。
Windows elevated sandbox の setup は `CODEX_HOME` ごとの単純な初期化ではない。
専用 local users、filesystem ACL、firewall/local policy と資格情報を扱うため、同一 Windows
上で別 home を setup すると、既定 home と broker home のどちらかを stale にし得る。

Codex CLI 0.148.0 と Windows 11 の model-free probe では次を確認した。

1. 専用 broker home の setup は UAC 境界で停止した。model call は 0 件だった。
2. 既定 home の elevated credential plane を使う外側 sandbox は起動できた。
3. 外側 sandbox 内で `CODEX_HOME` を broker に切り替えることはできたが、同じ外側
   process boundary にいる child から broker/host auth path を読めた。
4. 外側 elevated sandbox と内側 unelevated sandbox の二重化は、内側が
   `Restricted read-only access requires the elevated Windows sandbox backend` と fail closed した。
5. この PC の WSL2 Ubuntu は `HCS_E_SERVICE_NOT_AVAILABLE` で起動できなかった。

このため、現在の Windows native CLI で「認証を持つ model control process」と
「model が起動する tool process」を、別 `CODEX_HOME` のまま安全かつ supported に分離する
構成は確認できなかった。

## Decision

### Native Windows

Real Model Benchmark は credential copy より前に fail closed する。

- stable code: `REAL_BENCHMARK_NATIVE_WINDOWS_BROKER_UNSUPPORTED`
- UAC/setupを起動しない
- `auth.json`、`.sandbox-secrets`、sandbox marker/stateを複製しない
- modelを呼ばない
- raw stderr、private path、credential metadataを返さない
- WSL2、隔離VM、macOS、Linuxでの実行を案内する

通常の RouteCraft orchestration、Graph observe、deterministic benchmark、doctor、security scan
は Windows native でも継続する。Real Benchmark の capability 不足で Runtime を停止しない。

### macOS / Linux / WSL

一時 broker home とOS sandboxを使用できる。ただし model-free preflight が全条件を
実測で通過した場合だけ model callを許可する。

- solver／acceptanceはbroker authとhost private homeを読めない。outer control processだけがbroker authを読める
- solverとouterだけがfixture workspaceへ書ける
- solver／outerはimmutable acceptance harnessを読めず、acceptanceだけがread-onlyで読める
- solver／acceptanceはdirect networkを拒否し、model API用outerだけがnetworkを使用できる
- unified RouteCraft Pluginがちょうど1件で、source/versionがbroker内manifestと一致する
- `deny`をcanonical filesystem access valueとして使う

### Cross-device

Codex/GitHub authenticationとWindows sandbox credentialは各端末で作る。配布物、Private
Decision Store、Graph State、Memory Local backupを経由して共有しない。Windows setupは
Codex既定homeに対する人間操作であり、RouteCraft installerは自動実行しない。

## Rejected alternatives

### 専用 Windows broker homeをUAC setupする

固定sandbox accountのcredentialを機械全体で更新し得るため不採用。既定homeとの独立性を
証明できない。

### `.sandbox-secrets`をcopy、junction、symlinkで共有する

unsupportedな永続形式への依存であり、privacy、upgrade、rollback境界を破るため不採用。

### outer sandbox内でinner Codexのsandboxを完全bypassする

inner Codexが読むbroker authをmodel tool childも読めるため不採用。

### permission profileの`deny`だけをcredential boundaryとみなす

0.148.0 model-free raw-child probeでread denialを証明できなかった。preflightは設定値ではなく
実際のread failureを要求する。

## Consequences

- 現在のWindows desktopでは6-run Pilotを実施しない。結果は
  `INSUFFICIENT EVIDENCE` のまま保持する。
- MacBook、Linux、正常なWSL2/VMでは、同じ6-run上限をpreflight後に実施できる。
- native Windows supportは、OpenAIがseparate credential store／broker contractを提供するか、
  model-free probeで同等の境界を証明できた時だけrevisionで追加する。
- 本DecisionはProduction Policy、Graph allowlist、Control Center、D1を変更しない。

## Primary sources

- [OpenAI Codex Windows sandbox](https://learn.chatgpt.com/docs/windows/windows-sandbox)
- [OpenAI Codex permissions](https://learn.chatgpt.com/docs/permissions)
- [OpenAI Codex non-interactive mode](https://learn.chatgpt.com/docs/noninteractive)
- [OpenAI Codex sandbox setup source](https://github.com/openai/codex/blob/rust-v0.148.0/codex-rs/windows-sandbox-rs/src/setup.rs)
- [OpenAI Codex Windows sandbox users source](https://github.com/openai/codex/blob/rust-v0.148.0/codex-rs/windows-sandbox-rs/src/bin/setup_main/win/sandbox_users.rs)
