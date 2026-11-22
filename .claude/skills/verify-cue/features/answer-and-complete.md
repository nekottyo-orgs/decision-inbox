# 端末での回答から処理完了まで

端末で回答した判断を処理側が受け取り、完了を Web API から確認する。

## Sub-features

- PTY の実 TUI で先頭候補を Enter で選択
- watch 通知、claim、complete の判断 ID を照合
- Web API の回答と状態を検査

## How to get to it (user POV)

- `tui` の判断待ちで候補を選び、Enter で回答
- 処理側の `watch`、`claim`、`complete` で回答を処理
- Web API の `GET /api/decisions/<判断 ID>` で現在の状態を表示

## Driving it with verify.py

Preconditions: `verify.py tui` の前半で検証依頼を登録。HTTP API と PTY が利用可能。

- 画面の題名 `Cue verification request` を確認し、Enter。画面の回答保存通知と HTTP の `choice: first`、`status: pending` を照合
- `watch` の `cue.ready` に登録時の ID が 1 件出現
- `claim --worker verify-cue --token-file <一時ファイル>` と HTTP の両方で同一 ID の `processing` を確認
- `complete --id <判断 ID> --token-file <一時ファイル>` と HTTP の両方で同一 ID、同一回答、`completed` を確認

## Gotchas

- 先頭の候補と推奨候補は異なるため、完了状態に加えて `choice` も照合
- `watch` の通知後に処理権を claim
