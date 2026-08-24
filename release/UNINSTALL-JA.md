# アンインストール

RouteCraft Memory LocalはOSへservice、常駐process、registry、login itemを登録しません。

1. 起動中の `routecraft ui` を `Ctrl+C` で終了します。
2. 必要なら `routecraft backup` またはproject exportでデータを退避します。
3. 展開した配布フォルダをゴミ箱へ移動します。
4. データも不要な場合だけ、既定の `~/.routecraft-memory-local/` を利用者自身で削除します。

データフォルダの削除は復元できません。ZIPを削除しただけでは利用者DBは消えません。
