# RouteCraft for Codex

RouteCraftは、**Solを設計・統合・最終判断に残し、実装だけを必要に応じてLuna/Terraへ振り分ける**Codex向けオーケストレーション・プラグインです。

狙いは「サブエージェントをたくさん使うこと」ではありません。

**小さい仕事はSol単独。委譲するなら最安で完遂できるlane。並列化は独立作業だけ。最後は必ず親Solがdiffとテストを確認。高リスク時だけFresh Solレビュー。**

## 基本構成

```text
Sol / High
  ├─ SOLO: 小さい仕事はそのまま実装
  ├─ DELEGATE
  │    ├─ Luna low / medium / max
  │    └─ Terra medium / high
  ├─ PARALLEL: 独立した2～3作業だけ並列
  └─ Parent verification
          └─ 高リスク時のみ Fresh Sol / High review
```

## 重要な特徴

- `solo` を標準にして、委譲オーバーヘッドを抑える
- モデルと推論強度をタスクごとに最適化する
- 子エージェントと親Solで同じ実装を重複させない
- 子の変更ファイルを明示し、並列時の競合を抑える
- 親Solが実diffとテストを再確認する
- 認証・決済・データ移行などはFresh Solレビューを追加する
- Codexのspawn機能がモデル指定に対応していない場合、安価なlaneを使ったふりをせずsoloへ戻す

## 導入

公開後は次の形で導入できます。

```sh
codex plugin marketplace add tamas-hub/codex-routecraft --ref main
codex plugin add codex-routecraft@routecraft
```

続けてCustom Agentをインストールします。詳細は英語READMEと `docs/INSTALL.md` を参照してください。

## 使い方

Sol / Highで新規タスクを開始し、例えば次のように指示します。

```text
Use $codex-routecraft:orchestration to implement this task. Choose the cheapest safe lane, parallelize only independent work, verify the complete diff, and use fresh Sol review only when risk warrants it.
```

## コストについて

固定の「○%削減」はうたいません。実際の削減量は、Luna/Terraへ移せる実装比率、推論強度、コンテキスト量、再試行、検証、レビュー頻度で変わります。

RouteCraftは、**高価なSol処理を無理に減らすのではなく、Solでやる必要のない実装だけを安全に移す**ことを目的にしています。

## License

MIT
