# 依頼の登録

判断する人に候補を表示するため、依頼側が Cue に登録する。

## Sub-features

- JSON の依頼を CLI で登録
- 発行された判断 ID を Web API で照合

## How to get to it (user POV)

- CLI の `add --file` で JSON を投入
- Web API の `GET /api/decisions/<判断 ID>` で登録した依頼を表示

## Driving it with verify.py

Preconditions: 空 DB で起動と読み取り点検に成功。

- `verify.py tui` の `add --file request.json` が検証用の依頼を登録
- `run.jsonl` の `add` の終了コードが 0、同じ ID の HTTP 200 の応答で `answer: null`

## Gotchas

- 登録には候補ごとの利点と懸念、推奨候補と推奨理由が必須
