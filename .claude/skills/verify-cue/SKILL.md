---
name: verify-cue
description: Cue (decision-inbox) の CLI・TUI・HTTP API、ブラウザの実機検証。該当機能の変更時や Mac への反映後、同じ判断 ID の処理や Web 操作を確認するときに使う。
---

# Cue の動作確認

[機能マップ](features/README.md) から操作を選ぶ。初回の実証は、CLI 登録から TUI の Enter、watch、claim、complete、Web API までの経路と、別の空 DB で実行する既存の Web 自己検査。

## 起動

repo のルートで実行する。macOS または Linux の Python 3.9 以降と curses が必要。TUI 経路は Web のビルドを必要としない。Web の場合は [README の起動手順](../../../README.md#起動) に従い、Node.js で UI をビルドしてから `agent-browser` と Chrome / Chromium を用意する。

```sh
run_evidence=$(mktemp -d /tmp/cue-proof.XXXXXXXX)
.claude/skills/verify-cue/verify.py tui "$run_evidence/tui"

npm --prefix frontend ci
npm --prefix frontend run build
CUE_VERIFY_CHROME='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' \
  .claude/skills/verify-cue/verify.py web "$run_evidence/web"
```

macOS の Chrome が別の場所にある場合や Linux では、`CUE_VERIFY_CHROME` に対象ブラウザの実行ファイルの絶対パスを指定する。TUI と Web は別の一時 HOME・DB を作り、`app.py --db <一時DB> serve --port 0` の出力からその回の loopback URL を得る。Web は専用のブラウザセッション・ソケット名前空間・プロファイルで起動する。Herdr の接続先も一時領域に向ける。

## 読み取り点検

書き込み前の点検は helper が実行する。対象の commit と Python、CLI の実体を `run.jsonl` に記録し、`hooks` が `{"pre_add":null}`、起動したプロセスの `GET /api/decisions` が HTTP 200 かつ空であることを照合する。helper と依頼 JSON を含む実行対象の追跡状態と内容を対象 commit と照合する。前提不足の場合は実行対象をコミットして版を確定する。

Mac に反映した版を確認する場合は、`command -v decision-inbox`、`readlink ~/.local/bin/decision-inbox`、`readlink ~/.local/share/cue/current`、`decision-inbox hooks` で配線を読み、反映先と検証対象の commit を照合する。最後のコマンドは DB を開かない。配線の観測は隔離環境での操作結果と一緒に記録する。

## 操作と判定

- TUI 経路では [request.json](request.json) を `add --file` で投入し、PTY の画面で対象の題名を待ってから Enter。通知と処理は [回答と完了](features/answer-and-complete.md) の条件で判定
- Web は空 DB で [既存の自己検査](../../../tests/self-check.sh) を実行。表示・回答・取り消し・処理状態の合格行は `web.log` で確認
- 終了コード `0` は合格、`1` は確認に失敗、`2` は前提不足。起動時に loopback の bind や PTY を作れない環境では、その制約を記録し、対象の操作を未確認とする

## 証跡

指定した新しいディレクトリに `run.jsonl`、`server.stderr`、TUI の `tui.ansi` または Web の `web.log` を保存する。`run.jsonl` の操作、対象版・環境、同一 ID の API 応答と状態、終了コード、未確認範囲を一緒に確認する。TUI の記録は ANSI 制御文字を含むため、`strings` 等で閲覧する。claim token は一時ファイルに保存し、証跡には含めない。

## 後片付け

終了時に起動したプロセスと Web の専用セッションを止め、一時 HOME・DB・token・プロファイルを削除する。Web の合格には、自己検査でのブラウザ終了と、専用セッションの `session info --json` が `active: false` と `pid: null` を返すことが必要。終了確認は `run.jsonl` に残る。失敗時も `cleanup` が `state_removed: true` であることと、証跡ファイルが読めることを確認する。
