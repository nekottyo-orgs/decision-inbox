# claim の所有権と再試行

回答済みの判断 1 件について、claim による処理開始、期限切れ後の再取得、renew、report、明示的な retry、complete を [Lean](ClaimLease.lean) で検査する。1 件に同時に有効な token は 1 つまで。有効な claim を上書きできず、期限切れまたは別の token による complete・report・renew は拒否される。renew は token を維持し、期限を短くしない。停止後の再試行には現在の報告が必要で、完了は再試行や再取得の対象外。

## 実装との対応

| モデル | 実装 |
| --- | --- |
| `claim` と `processing` | `Store.claim` が書き込みロックを取得してから、pending または期限切れの行を選び、token と期限を更新 |
| `finishClaim` | commit 前の失敗では元の行に戻る。commit が成功したら更新後の行を保持し、その直後に現在時刻が期限未満のときだけ取得成功を返す。期限ちょうどから新しい token で再取得可能 |
| `active`、`complete`、`report`、`renew` | `Store.active` が状態・token・期限を照合。各書き込みは同じ transaction 内で行い、renew は現在の期限と要求期限の最大値を使用 |
| `retry` | `Store.retry` が現在の回答と停止報告の時刻を照合して pending に戻す。モデルでは回答を固定し、その回答に対する停止報告を claim の世代で表現 |

前提は、SQLite の `BEGIN IMMEDIATE` が書き込みを直列化し、発行された token が再利用されないこと。時刻は境界 `now < leaseUntil` を保つ自然数で表す。blocked と failed は所有権に関して同じ扱いなので一つの状態にまとめる。停止報告は 1 claim につき最大 1 回で、再試行後の新しい claim とは区別される。

検査対象は所有権に関する状態遷移。選択順序、人による close、時刻の巻き戻り、token ファイル、外部処理の冪等性は、API の規約と実装テストで扱う。

## 検証

リポジトリのルートで実行する。証明の型検査に加え、サンプルの 1 件が claim、期限切れ、再取得、短い renew、停止、再試行、完了まで進むことを `#guard` で検査する。

```sh
lean +leanprover/lean4:v4.36.0 proofs/ClaimLease.lean
lean +leanprover/lean4:v4.36.0 proofs/TaskAssignment.lean
lean +leanprover/lean4:v4.36.0 proofs/DecisionSnapshot.lean
lean +leanprover/lean4:v4.36.0 proofs/SessionRegistry.lean
lean +leanprover/lean4:v4.36.0 proofs/ReleaseSwitch.lean
python3 -m unittest discover -s tests -p 'test_*.py'
```

実装テストでは、SQLite の独立した接続から競合して claim したときの勝者、読み取りロックによる commit 待ちでの失効、commit 失敗時の元の行、再取得後の旧 token、古い停止報告、短い renew を照合する。モデルの時刻は commit 直後の照合時点で、応答の転送時間は含まない。関連ファイルの変更では、CI が上記と同じ証明と実装テストを実行する。


## 担当を更新する条件

[TaskAssignment.lean](TaskAssignment.lean) は、旧 lane・pane・machine と、指定があれば旧 run・request・kind が一致したときだけ担当を更新できることを検査する。一致後に同じ担当を指定すると、モデル上の revision も含めて行はそのままになる。担当が変わる場合も、所属と remaining は保持する。

`Store.reconcile_lane` は `BEGIN IMMEDIATE` で SQLite の書き込みを直列化し、比較条件を `UPDATE` の `WHERE` に入れる。同値の確認も同じ transaction 内で行い、実装では `updated_at` を進めず既存の行を返す。実装テストでは 6 項目それぞれの不一致、空文字の比較、同じ行を観測した 2 つの CLI の競合を検査する。モデルの範囲は Cue 内の 1 行。Herdr の pane 起動・終了と一体の操作にはならない。

## 判断の一覧と詳細の読み取り

[DecisionSnapshot.lean](DecisionSnapshot.lean) は、最初の読み取りから応答までに他の接続が何回確定しても、一覧 (履歴から先に読む) と詳細 (本文から先に読む) の本文・revision・履歴が最初の読み取り時点と一致することを検査する。revision は更新履歴の件数とする。

`Store.list` と `Store.get` の `BEGIN` から、SQLite が最初の SELECT のスナップショットをトランザクション終了まで保持することを前提とする。モデルは判断 1 件の本文と更新履歴を扱う。JSON の項目、並び順、取り下げ、古い revision への回答拒否と、実際の 2 接続の動作は Python の回帰テストで検査する。

## セッション所属の開閉と一意性

[SessionRegistry.lean](SessionRegistry.lean) は、Claude・Codex の窓口・worker・Cue 専任 worker の所属記録 (`sessions` 表) について、`open` と `close` の状態遷移を検査する。開いた状態 (`closed_at IS NULL`) で `(cli, native_session_id)` と `(run_id, role, pane, socket)` がそれぞれ一意であること、重複する `open` が拒否されること、閉じた行が後続の `open`・`close` で書き換わらないこと、`close` が `id`・`run_id`・`role`・`req_id`・`pane`・`socket`・`cli`・`native_session_id`・`opened_at` を保持したまま終了時刻と理由だけを入れること、未登録・既閉鎖の `close` が拒否されることを証明し、`#guard` で一連の開閉を検査する。

| モデル | 実装 |
| --- | --- |
| `openSession` と `WellFormed` | `Store.open_session` が `BEGIN IMMEDIATE` で書き込みを直列化し、重複を検査して挿入。SQLite の部分一意索引 `sessions_open_native (cli, native_session_id) WHERE closed_at IS NULL` と `sessions_open_slot (run_id, role, pane, socket) WHERE closed_at IS NULL` でも一意性を担保 |
| `closeSession` (`byNative` / `bySlot`) | `Store.close_session` が `BEGIN IMMEDIATE` 内で `closed_at IS NULL` の一致行を引き、0 件なら 404、複数件 (`--pane` / `--socket` / `--run-id` で role が重複する場合) なら 409 で拒否し、1 件のときだけ `closed_at` と `close_reason` を更新 |
| `Closure` (`closedAt`, `reason`) | 開いた行は `closed_at IS NULL` かつ `close_reason = ''`、`close` 時にのみ `closed_at` (ISO 8601) と `close_reason` (`normal` / `replaced` / `crashed` / `closed`) を同時設定 |

前提は、SQLite の `BEGIN IMMEDIATE` が書き込みを直列化すること、部分一意索引 `WHERE closed_at IS NULL` が開いた行にだけ効くこと、および `INTEGER PRIMARY KEY` が一意な単調 ID を割り当てること。時刻は自然数で表す。文字列長・サロゲート・ISO 8601 の解析、`session list` の絞り込み (`--run-id` / `--cli` / `--open-only` / `--since` / `--until`)、SQLite の 2 接続競合、および `PRAGMA user_version = 2` を保った旧 release DB 互換は `tests/test_sessions.py` で検査する。


## 公開 release の切り替え

[ReleaseSwitch.lean](ReleaseSwitch.lean) は事前検証の成功後にだけ候補を有効にできること、事前検証の失敗で現行 release が残ること、切り替え後の検査に失敗した場合に復旧が成功すれば旧 release に戻ることを扱う。更新コマンドではファイルロックにより一度に一件だけ更新を進め、`current` の入れ替えに同一ファイルシステム内の atomic rename を使う。再起動した PID と Web の待ち受けが一致しない観測は成功への遷移に進めない。SHA-256、アーカイブの検証、SQLite の互換性、Web と Herdr の確認は更新コマンドを入口とする Python の回帰テストで扱う。プロセスの強制終了、他の手段による同時書き換え、外部サービスの恒久障害はモデルの対象外。別の参照先を観測したときは処理を停止し、復元を含む後続の書き換えは行わない。
