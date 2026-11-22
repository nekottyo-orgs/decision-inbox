# ブラウザでの判断

Web で依頼を読み、候補とメモを保存して後続の状態を確認する。

## Sub-features

- 一覧、対象への直接リンク、背景と候補の表示
- 回答、取り消し、保留、競合時の入力保持
- 処理完了・停止・再処理の表示とモバイルの表示

## How to get to it (user POV)

- Cue の Web 画面をブラウザで開き、一覧から対象を選択
- 判断済み画面で回答と処理状態を確認

## Driving it with verify.py

Preconditions: ビルド済みの Web、`agent-browser`、Chrome または Chromium、別の空 DB。

- `verify.py web` が既存の `tests/self-check.sh` を専用ブラウザで実行
- `web.log` の「合格：API 接続の UI self-check 全項目」と、自己検査の終了コード 0 を確認

## Gotchas

- 既存の自己検査は検証用依頼を自分で登録するため、TUI 経路と DB を分離
- 保存失敗と競合のうち、保存失敗だけは既存検査がブラウザ内で応答を差し替えて確認
