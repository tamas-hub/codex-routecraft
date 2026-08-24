# RouteCraft Memory Local v1.0 実装計画

更新日: 2026-08-23

## 現状調査の結論

- 現行リポジトリは `codex-routecraft` であり、既存の Persistent Decision Layer は Markdown の Case / Candidate / Rule、限定 Recall、Learn、Promotion、Private Git 同期を提供している。
- 現行 `routecraft_memory.py` と schema version 1 は既存利用者向けの互換面として維持する。
- プロジェクト管理、汎用の構造化メモリ、Context Pack、Handoff Pack、ローカル Web UI、SQLite バックアップ／復元、配布 ZIP は新規機能である。
- 既存検証は `scripts/verify.py` と `python -m unittest discover -s tests -v` で成功している。Windows の日本語 stdin 回帰テストも存在するため、新 CLI でも同等以上の回帰テストを追加する。

## v1.0 の設計判断

1. 既存 Decision Store を置換せず、新規 `routecraft_local` 名前空間へ追加する。
2. Python 3.11 以上の標準ライブラリだけを使用し、有料 API、Docker、常駐監視、クラウドを要求しない。
3. 新規データは既存 Store と分離した SQLite DB に保存する。DB schema は `PRAGMA user_version` で管理し、将来の migration 前には自動バックアップする。
4. 既存 Markdown Store は読み取り import で移行し、元データを変更しない。legacy ID と content hash を保持し、再 import と競合を検出する。
5. ローカル UI は `127.0.0.1` のみに bind する標準ライブラリ HTTP server と、依存のない HTML/CSS/JavaScript SPA で提供する。
6. Git 連携は status、diff 統計、log、tag、remote の読み取りだけを実装し、commit、branch、push は実行しない。
7. 保存・export・Handoff の各境界で秘密情報を検出してマスキングする。`.env`、鍵、token、credential、個人情報候補、除外 path を既定で取り込まない。
8. 配布物は Windows / macOS 向け ZIP とし、Python 本体は同梱しない。署名・notarization は行わず、その制約を明記する。
9. テレメトリは無効が初期値であり、RouteCraft Memory Local 自体は外部通信コードを持たない。

## 実装モジュール

| 領域 | 主な配置 | 完成条件 |
|---|---|---|
| DB・migration・安全性 | `plugins/codex-routecraft/scripts/routecraft_local/core.py` | project / memory / import conflict schema、FTS5、migration 前 backup、integrity check、masking |
| アプリケーションサービス | `routecraft_local/service.py` | CRUD、検索 filter、import/export、backup/restore、明示確認 |
| Git と Pack | `routecraft_local/git_tools.py`, `packs.py` | read-only Git、rule-based summary、Context/Handoff、文字数上限、token 概算 |
| CLI | `routecraft_local/cli.py`, `scripts/routecraft.py` | 要求コマンド、help、JSON、人間向け出力、終了コード、日本語 stdin |
| Web UI | `routecraft_local/ui.py`, `routecraft_local/web/` | 日本語 10 画面相当、CRUD、検索、Pack、Git、backup、settings、doctor、localhost only |
| 評価・サンプル | `samples/`, `scripts/evaluate_routecraft_local.py` | 正解データ、Hit@K / MRR、1,000 件性能、重複抑制 |
| 配布 | `scripts/build_local_release.py`, `release/` | Windows/macOS ZIP、SHA256、初回起動・削除手順、VERSION、notices |
| 仕様・運用文書 | `docs/*-v1.md`, README、CHANGELOG | 指定された 6 文書、既知制約、復旧、BOOTH 前確認 |

## 実装順序

1. DB schema、migration、安全な CRUD と検索契約を固定する。
2. Git 読み取り、Context/Handoff、import/export、backup/restore を実装する。
3. CLI を統合し、日本語 stdin / BOM / CRLF / pipe / input-file を検証する。
4. 同じ service 層を呼ぶローカル Web UI を実装する。
5. 既存 Store migration、サンプル、評価ハーネス、1,000 件性能試験を追加する。
6. Windows / macOS 配布 ZIP と checksums を生成する。
7. 既存 suite と新規 suite、実 UI の 375px / desktop、配布物 smoke test を再実行する。
8. 高リスク領域を fresh reviewer が読み取りレビューし、指摘修正後に再検証する。

## 合理的な仮定

- v1.0 は Python 3.11 以上と Git（Git 情報を使う場合）を前提にし、実行ファイル化は将来版とする。
- macOS の実機は現在利用できないため、OS 非依存テスト、shell syntax、ZIP 内容、パス処理を Windows 上で検証し、実機未検証を明記する。
- 完全な日本語形態素解析は行わず、FTS5 と Unicode 部分一致、空白区切り、タグ、重要度・新しさの再 ranking を組み合わせる。
- Web UI は一人の利用者が同一端末で操作する構成で、アカウントとネットワーク共有は対象外とする。
- 既存 Decision Store の Git 同期はそのまま残すが、新 SQLite DB を Git へ自動追加しない。持ち運びは安全化した project package / JSONL を使う。

## v1.0 対象外

- 独自クラウド同期、共同編集、課金・認証サーバ、AI API 必須機能、IDE 専用 plugin、ローカル LLM 同梱、ベクトル DB、常駐監視、リモート実行、自動 Git 書き込み。

## 受け入れ証拠

- 既存 `scripts/verify.py` と既存全 unit tests が引き続き成功する。
- 新規 unit / DB / migration / CLI / search / pack / Git / masking / backup / encoding / UI / invalid-input tests が成功する。
- 1,000 memories の投入・検索・一覧がテスト上の実用的な時間内で完了する。
- Web UI を 127.0.0.1 で起動し、約 375px と desktop 幅で主要操作、長い日本語、focus、console error を確認する。
- 2 種類の ZIP を clean temporary directory で展開し、`--help`、demo import、search、Context/Handoff を smoke test する。
- ZIP の SHA256 と tracked release manifest が一致する。
