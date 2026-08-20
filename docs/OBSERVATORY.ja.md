# RouteCraft Observatory

RouteCraftの3台＋α構成、GitHub同期、Persistent Decision Memoryの状態をWebから監視するための軽量ダッシュボードです。

## 推奨構成

本番監視はXserver modeを推奨します。

```text
Windows #1 ─┐
Windows #2 ─┼─ sanitized heartbeat ─→ Xserver PHP API ─→ Observatory Web
Mac #3     ─┤
+α         ─┘

各端末
├─ ~/codex-routecraft
├─ ~/routecraft-memory
└─ ~/.codex
```

Web画面は15秒ごと、端末heartbeatは5分ごとの更新を標準とします。

## 監視対象

- 端末 Online / Stale / Offline
- RouteCraft version
- public source HEAD / upstream HEAD / clean / ahead / behind
- Private Decision Store HEAD / conflict / clean
- Case / Candidate / Rule件数
- eligible Candidate件数
- Agent 6種類の原本一致
- plugin cache存在確認
- source divergence、memory conflict、heartbeat停止などのアラート

## Telemetry boundary

Heartbeatへ含めてよいのは、監視に必要な非秘密メタデータのみです。

含めません：

- `C:\\Users\\...`や`/Users/...`などの絶対ユーザーパス
- token、password、SSH/private key
- Git credential
- raw log
- prompt / conversation
- source code
- private email

`routecraft_observatory.py`はローカルのパスを使って状態を調べますが、送信payloadへパスそのものは含めません。

## Client

```sh
python plugins/codex-routecraft/scripts/routecraft_observatory.py --print
```

これで送信予定payloadだけを確認できます。

Xserver heartbeat endpointを用意した場合：

```sh
python plugins/codex-routecraft/scripts/routecraft_observatory.py \
  --endpoint https://example.com/routecraft/api/heartbeat.php \
  --token-file ~/.codex/routecraft/observatory/token \
  --alias Mac-1
```

## GitHub Pages mode

静的なsanitized status JSONを表示する用途には利用できますが、GitHub Pages単体では端末からheartbeatを受信できません。端末Online/Offlineを含む近リアルタイム監視はXserver modeを使用してください。

## Source of truth

Observatoryは監視専用です。状態を修復したり、Decision Storeの内容を書き換えたりする管理画面にはしません。

- RouteCraft sourceの正本: GitHub Public
- Decision Memoryの正本: GitHub Private
- 端末固有状態: 各端末local
- Observatory: 上記をsanitized telemetryとして可視化するだけ
