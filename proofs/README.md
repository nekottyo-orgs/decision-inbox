# claim の所有権と再試行

回答済みの判断 1 件について、claim による処理開始、期限切れ後の再取得、renew、report、明示的な retry、complete を [Lean](ClaimLease.lean) で検査する。1 件に同時に有効な token は 1 つまで。有効な claim を上書きできず、期限切れまたは別の token による complete・report・renew は拒否される。renew は token を維持し、期限を短くしない。停止後の再試行には現在の報告が必要で、完了は再試行や再取得の対象外。

## 実装との対応

| モデル | 実装 |
| --- | --- |
| `claim` と `processing` | `Store.claim` が書き込みロックを取得してから、pending または期限切れの行を選び、token と期限を更新 |
| `active`、`complete`、`report`、`renew` | `Store.active` が状態・token・期限を照合。各書き込みは同じ transaction 内で行い、renew は現在の期限と要求期限の最大値を使用 |
| `retry` | `Store.retry` が現在の回答と停止報告の時刻を照合して pending に戻す。モデルでは回答を固定し、その回答に対する停止報告を claim の世代で表現 |

前提は、SQLite の `BEGIN IMMEDIATE` が書き込みを直列化し、発行された token が再利用されないこと。時刻は境界 `now < leaseUntil` を保つ自然数で表す。blocked と failed は所有権に関して同じ扱いなので一つの状態にまとめる。停止報告は 1 claim につき最大 1 回で、再試行後の新しい claim とは区別される。

検査対象は所有権に関する状態遷移。選択順序、人による close、時刻の巻き戻り、token ファイル、外部処理の冪等性は、API の規約と実装テストで扱う。

## 検証

リポジトリのルートで実行する。証明の型検査に加え、サンプルの 1 件が claim、期限切れ、再取得、短い renew、停止、再試行、完了まで進むことを `#guard` で検査する。

```sh
lean +leanprover/lean4:v4.36.0 proofs/ClaimLease.lean
python3 -m unittest discover -s tests -p 'test_*.py'
```

実装テストでは、SQLite の独立した接続から競合して claim したときの勝者、期限ちょうどと再取得後の旧 token、古い停止報告、短い renew を照合する。関連ファイルの変更では、CI が上記と同じ証明と実装テストを実行する。
