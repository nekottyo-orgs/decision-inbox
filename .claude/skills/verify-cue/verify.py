#!/usr/bin/env python3
"""実際の Cue を一時環境で操作し、観測結果を保存する。"""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import select
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import time
from urllib.request import ProxyHandler, build_opener

SKILL = Path(__file__).resolve().parent
REPO = SKILL.parents[2]


class Unavailable(Exception):
    pass


def require(condition, message):
    if not condition:
        raise ValueError(message)


def journey(mode, evidence, state):
    def record(event, **fields):
        with (evidence / 'run.jsonl').open('a', encoding='utf-8') as log:
            print(json.dumps({'event': event, **fields}, ensure_ascii=False), file=log)

    def run(command, *, capture=True):
        record('command', argv=command)
        result = subprocess.run(command, cwd=REPO, env=env, capture_output=True,
                                text=True, encoding='utf-8', timeout=15)
        outputs = {'stdout': result.stdout, 'stderr': result.stderr} if capture else {}
        record('exit', command=command, code=result.returncode, **outputs)
        require(result.returncode == 0, f'コマンドが失敗しました: {shlex.join(command)}')
        return result.stdout

    @contextmanager
    def start(command, **streams):
        record('start', argv=command)
        with subprocess.Popen(command, cwd=REPO, env=env, **streams) as process:
            try:
                yield process
            finally:
                if process.poll() is None:
                    process.send_signal(signal.SIGINT)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                record('exit', command=command, code=process.returncode)

    def get(path):
        with http.open(url + path, timeout=5) as response:
            body = json.load(response)
            record('http', method='GET', url=url + path, status=response.status, body=body)
            return body

    def check_item(item, identifier, status):
        require(item['id'] == identifier, '判断 ID が一致しません')
        if status is None:
            require(item['answer'] is None, '初期状態が未回答ではありません')
        else:
            require(item['answer']['choice'] == 'first', 'TUI で選択した回答が一致しません')
            require(item['answer']['status'] == status, f'判断の状態が {status} ではありません')

    env = {
        'PATH': os.environ['PATH'],
        'HOME': str(state),
        'XDG_CONFIG_HOME': str(state / 'config'),
        'XDG_DATA_HOME': str(state / 'data'),
        'XDG_RUNTIME_DIR': str(state),
        'TMPDIR': str(state),
        'HERDR_SOCKET_PATH': str(state / 'herdr.sock'),
        'TERM': 'xterm-256color',
        'LANG': 'en_US.UTF-8',
        'PYTHONUTF8': '1',
        'PYTHONDONTWRITEBYTECODE': '1',
    }
    app = [sys.executable, str(REPO / 'app.py'), '--db', str(state / 'cue.sqlite3')]
    http = build_opener(ProxyHandler({}))
    try:
        revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip()
        record('environment', mode=mode, revision=revision, platform=platform.platform(),
               python=sys.version, cli=app, state=str(state), socket=env['HERDR_SOCKET_PATH'],
               unverified=['実 Herdr popup', '共有 DB', '実アカウント', '実モデル'] +
                          (['ブラウザ表示 (web で別に検証)'] if mode == 'tui' else
                           ['TUI の回答連結 (tui で別に検証)']))
        tracked_runtime = ['app.py', 'cue_tui.py', 'frontend', 'tests/self-check.sh']
        if subprocess.run(['git', 'diff', '--quiet', 'HEAD', '--', *tracked_runtime], cwd=REPO).returncode:
            raise Unavailable('実行対象に未コミットの変更があり、HEAD だけでは版を特定できません')
        with socket.socket() as can_bind:
            try:
                can_bind.bind(('127.0.0.1', 0))
            except PermissionError as error:
                raise Unavailable('この環境では loopback で待ち受けられません') from error
        if mode == 'web':
            chrome = Path(os.environ.get('CUE_VERIFY_CHROME', ''))
            if not (shutil.which('agent-browser') and chrome.is_absolute() and
                    chrome.is_file() and os.access(chrome, os.X_OK)):
                raise Unavailable('agent-browser と CUE_VERIFY_CHROME に実行ファイルが必要です')
            dist = REPO / 'frontend/dist'
            assets = ['index.html', 'assets/app.js', 'assets/app.css']
            if any(not (dist / name).is_file() for name in assets):
                raise Unavailable('先に README の手順で frontend をビルドしてください')
            empty_config = state / 'agent-browser.json'
            empty_config.write_text('{}\n', encoding='utf-8')
            env.update(AGENT_BROWSER_EXECUTABLE_PATH=str(chrome), AGENT_BROWSER_CONFIG=str(empty_config),
                       AGENT_BROWSER_PROFILE=str(state / 'chrome-profile'),
                       AGENT_BROWSER_NAMESPACE=state.name, AGENT_BROWSER_NO_VERSION_CHECK='1')
            record('browser', executable=str(chrome), profile=env['AGENT_BROWSER_PROFILE'],
                   namespace=env['AGENT_BROWSER_NAMESPACE'],
                   web_sha256={name: hashlib.sha256((dist / name).read_bytes()).hexdigest() for name in assets})
            run(['agent-browser', '--version'])

        require(json.loads(run(app + ['hooks'])) == {'pre_add': None}, '隔離環境に pre-add が存在します')
        with (evidence / 'server.stderr').open('wb') as errors:
            with start(app + ['serve', '--port', '0'], stdout=subprocess.PIPE, stderr=errors) as server:
                require(select.select([server.stdout], [], [], 10)[0], 'HTTP server の起動を確認できません')
                ready = server.stdout.readline().decode().strip()
                record('listening', pid=server.pid, stdout=ready)
                match = re.fullmatch(r'Listening on (http://127\.0\.0\.1:[1-9][0-9]*/)', ready)
                require(match is not None, '起動した server の loopback URL がありません')
                url = match[1]
                require(get('api/decisions') == {'decisions': [], 'demo': False}, '専用 DB の初期状態が空ではありません')
                if mode == 'web':
                    web_check(start, record, env, evidence, url)
                else:
                    request = SKILL / 'request.json'
                    item = json.loads(run(app + ['add', '--file', str(request)]))
                    identifier = item['id']
                    require(re.fullmatch(r'[a-f0-9]{32}', identifier), 'CLI が正しい判断 ID を返していません')
                    require(item['request_key'] == 'verify-cue/tui', '検証用の依頼と一致しません')
                    path = f'api/decisions/{identifier}'
                    check_item(get(path), identifier, None)

                    with start(app + ['watch', '--interval', '0.1', '--timeout', '30'],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as watcher:
                        answer_in_tui(start, record, evidence, app + ['tui', '--web-url', url])
                        check_item(get(path), identifier, 'pending')
                        require(select.select([watcher.stdout], [], [], 5)[0], 'watch の通知が届きません')
                        notification = watcher.stdout.readline().decode().strip()
                        record('watch', stdout=notification)
                        require(json.loads(notification) == {'event': 'cue.ready', 'decision_ids': [identifier]},
                                'watch の通知 ID が一致しません')

                    token_file = str(state / 'claim.json')
                    # token が誤って出力された場合にも、証跡には照合済みの値だけを残す。
                    claimed = json.loads(run(app + ['claim', '--worker', 'verify-cue', '--token-file', token_file],
                                             capture=False))
                    require('token' not in claimed and claimed['token_file'] == token_file,
                            'claim token の隔離を確認できません')
                    check_item(claimed['decision'], identifier, 'processing')
                    record('claim', id=claimed['decision']['id'], status=claimed['decision']['answer']['status'])
                    check_item(get(path), identifier, 'processing')
                    check_item(json.loads(run(app + ['complete', '--id', identifier, '--token-file', token_file])),
                               identifier, 'completed')
                    check_item(get(path), identifier, 'completed')
    except Unavailable as error:
        record('result', status='unavailable', code=2, message=str(error))
        return 2
    except (Exception, KeyboardInterrupt) as error:
        record('result', status='failed', code=1, message=f'{type(error).__name__}: {error}')
        return 1
    record('result', status='passed', code=0)
    return 0


def answer_in_tui(start, record, evidence, command):
    master, slave = os.openpty()
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 120, 0, 0))
        with (evidence / 'tui.ansi').open('wb') as transcript:
            with start(command, stdin=slave, stdout=slave, stderr=slave) as tui:
                for marker, key, label in [('Cue verification request', b'\r', 'Enter'),
                                           ('回答を保存しました', b'q', 'q')]:
                    output = bytearray()
                    deadline = time.monotonic() + 5
                    while marker.encode() not in output:
                        remaining = deadline - time.monotonic()
                        require(remaining > 0 and select.select([master], [], [], remaining)[0],
                                f'TUI に {marker} が表示されません')
                        chunk = os.read(master, 65536)
                        transcript.write(chunk)
                        output.extend(chunk)
                    record('key', screen=marker, input=label)
                    os.write(master, key)
                # curses の端末復帰が終わるまで PTY の出力を読み続ける。
                deadline = time.monotonic() + 5
                while tui.poll() is None and time.monotonic() < deadline:
                    if select.select([master], [], [], 0.1)[0]:
                        transcript.write(os.read(master, 65536))
                require(tui.poll() == 0, 'TUI が正常終了しませんでした')
    finally:
        os.close(master)
        os.close(slave)


def web_check(start, record, env, evidence, url):
    with (evidence / 'web.log').open('wb') as output:
        with start(['bash', str(REPO / 'tests/self-check.sh'), url], stdout=output, stderr=output) as check:
            try:
                require(check.wait(timeout=120) == 0, 'ブラウザ自己検査が失敗しました。web.log を参照してください')
            finally:
                if check.poll() is None:
                    # 自己検査が異常終了しても、その検査専用のブラウザだけを閉じる。
                    session = f'decision-inbox-api-selfcheck-{check.pid}'
                    subprocess.run(['agent-browser', '--session', session, 'close'], cwd=REPO, env=env,
                                   stdout=output, stderr=output, timeout=10)
            record('browser_check', log='web.log')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['tui', 'web'])
    parser.add_argument('evidence', type=Path, help='証跡専用の新しいディレクトリ')
    args = parser.parse_args()
    args.evidence.mkdir(mode=0o700)
    with tempfile.TemporaryDirectory(prefix='cv-', dir='/tmp') as directory:
        result = journey(args.mode, args.evidence, Path(directory))
    with (args.evidence / 'run.jsonl').open('a', encoding='utf-8') as log:
        print(json.dumps({'event': 'cleanup', 'state_removed': not Path(directory).exists()}), file=log)
    print(args.evidence.resolve(), flush=True)
    return result


if __name__ == '__main__':
    sys.exit(main())
