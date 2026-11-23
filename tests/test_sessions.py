from concurrent.futures import ThreadPoolExecutor
import contextlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import AppError, Store
from test_app import isolated_env, request_payload

ROOT = Path(__file__).resolve().parents[1]
# sessions 表を足す前の main (PR #46 の merge)。旧 release の app.py がそのまま動くかを確かめる基準
OLD_RELEASE = '6927e03'


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'sessions.sqlite3'
        self.store = Store(self.db)

    def status(self, call, *args, **kwargs):
        with self.assertRaises(AppError) as caught:
            call(*args, **kwargs)
        return caught.exception.status

    def cli(self, *arguments, script=None, db=None):
        return subprocess.run(
            [sys.executable, str(script or ROOT / 'app.py'), '--db', str(db or self.db), *arguments],
            capture_output=True, text=True, timeout=10, env=isolated_env(self.temp.name),
        )

    def open_payload(self, **changes):
        return {
            'run_id': 'run-1',
            'role': 'frontdesk',
            'pane': 'w1:p1',
            'socket': '/tmp/herdr.sock',
            'cli': 'claude',
            'native_session_id': 'uuid-claude-1',
            **changes,
        }

    def test_open_と_close_で項目を保持し閉じた行は不変(self):
        opened = self.store.open_session(self.open_payload(role='worker', req_id='req-42'))
        self.assertEqual(opened['run_id'], 'run-1')
        self.assertEqual(opened['role'], 'worker')
        self.assertEqual(opened['req_id'], 'req-42')
        self.assertEqual(opened['pane'], 'w1:p1')
        self.assertEqual(opened['socket'], '/tmp/herdr.sock')
        self.assertEqual(opened['cli'], 'claude')
        self.assertEqual(opened['native_session_id'], 'uuid-claude-1')
        self.assertIsNotNone(opened['opened_at'])
        self.assertIsNone(opened['closed_at'])
        self.assertEqual(opened['close_reason'], '')

        closed = self.store.close_session(cli='claude', native_session_id='uuid-claude-1', reason='normal')
        self.assertIsNotNone(closed['closed_at'])
        self.assertEqual(closed['close_reason'], 'normal')
        for key in ('id', 'run_id', 'role', 'req_id', 'pane', 'socket', 'cli', 'native_session_id', 'opened_at'):
            self.assertEqual(closed[key], opened[key])

        # 閉じたあとに同じ slot・native_session_id を開き直しても旧行は不変
        reopened = self.store.open_session(self.open_payload(role='worker', req_id='req-43'))
        self.assertNotEqual(reopened['id'], opened['id'])
        self.assertEqual(self.store.sessions()['sessions'], [closed, reopened])
        # 開き直した行を閉じても、最初に閉じた行の closed_at / close_reason は変わらない
        closed2 = self.store.close_session(cli='claude', native_session_id='uuid-claude-1', reason='replaced')
        self.assertEqual(closed2['id'], reopened['id'])
        self.assertEqual(self.store.sessions()['sessions'], [closed, closed2])

    def test_全ての_role_cli_close_reason_を受け付ける(self):
        for idx, role in enumerate(('frontdesk', 'worker', 'cue_worker')):
            cli_name = 'claude' if idx % 2 == 0 else 'codex'
            row = self.store.open_session(self.open_payload(
                role=role, cli=cli_name, pane=f'p{idx}', native_session_id=f'sid-{idx}',
            ))
            self.assertEqual((row['role'], row['cli'], row['req_id']), (role, cli_name, ''))
        for idx, reason in enumerate(('normal', 'replaced', 'crashed', 'closed')):
            sid = f'reason-{idx}'
            self.store.open_session(self.open_payload(pane=f'rp{idx}', native_session_id=sid))
            closed = self.store.close_session(cli='claude', native_session_id=sid, reason=reason)
            self.assertEqual(closed['close_reason'], reason)

    def test_重複する開いた_open_を_native_と_slot_の両方で断り閉じれば開き直せる(self):
        self.store.open_session(self.open_payload())
        # 同じ (cli, native_session_id) は別 run / pane でも 409
        self.assertEqual(self.status(self.store.open_session, self.open_payload(run_id='run-2', pane='w2:p1')), 409)
        # 同じ (run_id, role, pane, socket) は別 cli / native_session_id でも 409
        self.assertEqual(self.status(self.store.open_session, self.open_payload(cli='codex', native_session_id='uuid-codex-1')), 409)
        # cli が違えば同じ native_session_id でも別 slot なら通る
        other_cli = self.store.open_session(self.open_payload(cli='codex', pane='w1:p2'))
        self.assertEqual(other_cli['cli'], 'codex')
        # role が違えば同じ (run_id, pane, socket) でも別 native_session_id なら通る
        other_role = self.store.open_session(self.open_payload(role='cue_worker', native_session_id='uuid-claude-2'))
        self.assertEqual(other_role['role'], 'cue_worker')

        # 閉じれば同じ (cli, native_session_id) も同じ (run_id, role, pane, socket) も開き直せる
        self.store.close_session(cli='claude', native_session_id='uuid-claude-1', reason='replaced')
        reopened = self.store.open_session(self.open_payload())
        self.assertIsNone(reopened['closed_at'])

    def test_部分一意索引が_SQLite_直書きの重複も防ぐ(self):
        self.store.open_session(self.open_payload())
        with contextlib.closing(sqlite3.connect(self.db)) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    'INSERT INTO sessions(run_id,role,pane,socket,cli,native_session_id,opened_at) VALUES(?,?,?,?,?,?,?)',
                    ('run-2', 'worker', 'p9', '/tmp/s2', 'claude', 'uuid-claude-1', '2026-11-23T00:00:00+00:00'),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    'INSERT INTO sessions(run_id,role,pane,socket,cli,native_session_id,opened_at) VALUES(?,?,?,?,?,?,?)',
                    ('run-1', 'frontdesk', 'w1:p1', '/tmp/herdr.sock', 'codex', 'uuid-other', '2026-11-23T00:00:00+00:00'),
                )

    def test_pane_socket_run_id_での_close_と複数一致時の絞り込み(self):
        first = self.store.open_session(self.open_payload(role='worker', native_session_id='sid-1'))
        closed = self.store.close_session(run_id='run-1', pane='w1:p1', socket='/tmp/herdr.sock', reason='crashed')
        self.assertEqual((closed['id'], closed['close_reason']), (first['id'], 'crashed'))
        # 既閉鎖の再度 close は 404
        self.assertEqual(self.status(self.store.close_session, run_id='run-1', pane='w1:p1', socket='/tmp/herdr.sock', reason='normal'), 404)

        # 同じ (run_id, pane, socket) に別 role が開いているときは --role 無しだと 409
        a = self.store.open_session(self.open_payload(role='frontdesk', native_session_id='sid-a'))
        b = self.store.open_session(self.open_payload(role='cue_worker', native_session_id='sid-b'))
        self.assertEqual(self.status(self.store.close_session, run_id='run-1', pane='w1:p1', socket='/tmp/herdr.sock', reason='closed'), 409)
        # どちらも閉じていない
        self.assertEqual(len(self.store.sessions(open_only=True)['sessions']), 2)
        closed_a = self.store.close_session(run_id='run-1', pane='w1:p1', socket='/tmp/herdr.sock', role='frontdesk', reason='closed')
        self.assertEqual(closed_a['id'], a['id'])
        closed_b = self.store.close_session(run_id='run-1', pane='w1:p1', socket='/tmp/herdr.sock', reason='normal')
        self.assertEqual(closed_b['id'], b['id'])

    def test_未知や既閉鎖の_close_を_404_で断る(self):
        self.assertEqual(self.status(self.store.close_session, cli='claude', native_session_id='missing', reason='normal'), 404)
        self.assertEqual(self.status(self.store.close_session, run_id='run-1', pane='p1', socket='/tmp/s', reason='normal'), 404)
        self.store.open_session(self.open_payload())
        closed = self.store.close_session(cli='claude', native_session_id='uuid-claude-1', reason='normal')
        self.assertEqual(self.status(self.store.close_session, cli='claude', native_session_id='uuid-claude-1', reason='crashed'), 404)
        self.assertEqual(self.store.sessions()['sessions'], [closed])

    def test_並行接続の重複_open_と二重_close_で片方だけが勝つ(self):
        def race(calls):
            gate = threading.Barrier(len(calls))

            def run(fn):
                separate = Store(self.db)
                gate.wait()
                try:
                    return ('ok', fn(separate))
                except AppError as error:
                    return ('err', error.status)

            with ThreadPoolExecutor(max_workers=len(calls)) as executor:
                return list(executor.map(run, calls))

        # 1. 同じ (cli, native_session_id) の並行 open
        native_results = race([
            lambda s: s.open_session(self.open_payload(pane='p1')),
            lambda s: s.open_session(self.open_payload(pane='p2')),
        ])
        self.assertEqual(sorted(tag for tag, _ in native_results), ['err', 'ok'])
        self.assertEqual([val for tag, val in native_results if tag == 'err'], [409])

        # 2. 同じ (run_id, role, pane, socket) の並行 open
        slot_results = race([
            lambda s: s.open_session(self.open_payload(run_id='run-2', native_session_id='sid-1')),
            lambda s: s.open_session(self.open_payload(run_id='run-2', native_session_id='sid-2')),
        ])
        self.assertEqual(sorted(tag for tag, _ in slot_results), ['err', 'ok'])
        self.assertEqual([val for tag, val in slot_results if tag == 'err'], [409])

        # 3. 同じ開いた session の並行 close
        close_results = race([
            lambda s: s.close_session(cli='claude', native_session_id='uuid-claude-1', reason='normal'),
            lambda s: s.close_session(cli='claude', native_session_id='uuid-claude-1', reason='replaced'),
        ])
        self.assertEqual(sorted(tag for tag, _ in close_results), ['err', 'ok'])
        self.assertEqual([val for tag, val in close_results if tag == 'err'], [404])

    def test_list_の絞り込みと時間窓フィルタ(self):
        stamps = iter([
            '2026-11-22T10:00:00.000000+00:00',  # s1 open (Nov 22)
            '2026-11-22T12:00:00.000000+00:00',  # s1 close (before window)
            '2026-11-22T23:00:00.000000+00:00',  # s2 open (spans into Nov 23)
            '2026-11-23T02:00:00.000000+00:00',  # s2 close (inside window)
            '2026-11-22T23:30:00.000000+00:00',  # s3 open (spans into Nov 23, still open)
            '2026-11-23T05:00:00.000000+00:00',  # s4 open (inside window)
            '2026-11-23T06:00:00.000000+00:00',  # s4 close (inside window)
            '2026-11-23T22:00:00.000000+00:00',  # s5 open (inside window, closes after)
            '2026-11-24T01:00:00.000000+00:00',  # s5 close (after window)
            '2026-11-24T02:00:00.000000+00:00',  # s6 open (after window)
        ])
        with patch('app.utc_now', side_effect=lambda: next(stamps)):
            s1 = self.store.open_session(self.open_payload(run_id='r1', cli='claude', pane='p1', native_session_id='s1'))
            self.store.close_session(cli='claude', native_session_id='s1', reason='normal')
            s2 = self.store.open_session(self.open_payload(run_id='r1', cli='codex', pane='p2', native_session_id='s2'))
            self.store.close_session(cli='codex', native_session_id='s2', reason='replaced')
            s3 = self.store.open_session(self.open_payload(run_id='r2', cli='claude', pane='p3', native_session_id='s3'))
            s4 = self.store.open_session(self.open_payload(run_id='r2', cli='codex', pane='p4', native_session_id='s4'))
            self.store.close_session(cli='codex', native_session_id='s4', reason='normal')
            s5 = self.store.open_session(self.open_payload(run_id='r1', cli='claude', pane='p5', native_session_id='s5'))
            self.store.close_session(cli='claude', native_session_id='s5', reason='closed')
            s6 = self.store.open_session(self.open_payload(run_id='r2', cli='codex', pane='p6', native_session_id='s6'))

        ids = lambda **kw: [row['native_session_id'] for row in self.store.sessions(**kw)['sessions']]
        self.assertEqual(ids(), ['s1', 's2', 's3', 's4', 's5', 's6'])
        self.assertEqual(ids(run_id='r1'), ['s1', 's2', 's5'])
        self.assertEqual(ids(cli='codex'), ['s2', 's4', 's6'])
        self.assertEqual(ids(open_only=True), ['s3', 's6'])
        self.assertEqual(ids(run_id='r2', cli='claude', open_only=True), ['s3'])

        # 2026-11-23 UTC 窓と重なるのは s2, s3, s4, s5 (s1 は窓前に閉鎖、s6 は窓後に開始)
        self.assertEqual(
            ids(since='2026-11-23T00:00:00Z', until='2026-11-23T23:59:59Z'),
            ['s2', 's3', 's4', 's5'],
        )
        # +09:00 表記も同じ UTC 時点として扱う (2026-11-23T09:00:00+09:00 == 2026-11-23T00:00:00Z)
        self.assertEqual(
            ids(since='2026-11-23T09:00:00+09:00', until='2026-11-24T08:59:59+09:00'),
            ['s2', 's3', 's4', 's5'],
        )
        # since のみ: 閉鎖前の s1 を除外
        self.assertEqual(ids(since='2026-11-23T00:00:00Z'), ['s2', 's3', 's4', 's5', 's6'])
        # until のみ: 窓後に開始した s6 を除外
        self.assertEqual(ids(until='2026-11-23T23:59:59Z'), ['s1', 's2', 's3', 's4', 's5'])
        self.assertEqual((s1['id'], s2['id'], s3['id'], s4['id'], s5['id'], s6['id']), (1, 2, 3, 4, 5, 6))

    def test_不正な入力は_400_で断る(self):
        broken = b'/tmp/\xff'.decode(errors='surrogateescape')
        for bad_changes in (
            {'role': 'orchestrator'},
            {'role': ''},
            {'cli': 'gemini'},
            {'run_id': ' '},
            {'pane': 'x' * 201},
            {'socket': broken},
            {'native_session_id': 'a\x00b'},
            {'req_id': 'x' * 201},
            {'extra': 'nope'},
        ):
            with self.subTest(open_bad=bad_changes):
                self.assertEqual(self.status(self.store.open_session, self.open_payload(**bad_changes)), 400)

        for missing in ('run_id', 'role', 'pane', 'socket', 'cli', 'native_session_id'):
            payload = self.open_payload()
            del payload[missing]
            self.assertEqual(self.status(self.store.open_session, payload), 400)

        for close_kwargs in (
            {'cli': 'claude', 'native_session_id': 'u', 'reason': 'invalid'},
            {'cli': 'claude', 'native_session_id': 'u', 'reason': ''},
            {'reason': 'normal'},
            {'cli': 'claude', 'reason': 'normal'},
            {'native_session_id': 'u', 'reason': 'normal'},
            {'cli': 'gemini', 'native_session_id': 'u', 'reason': 'normal'},
            {'pane': 'p1', 'socket': '/tmp/s', 'reason': 'normal'},
            {'pane': 'p1', 'run_id': 'r1', 'reason': 'normal'},
            {'socket': '/tmp/s', 'run_id': 'r1', 'reason': 'normal'},
            {'cli': 'claude', 'native_session_id': 'u', 'pane': 'p1', 'socket': '/tmp/s', 'run_id': 'r1', 'reason': 'normal'},
            {'pane': 'p1', 'socket': '/tmp/s', 'run_id': 'r1', 'role': 'bad', 'reason': 'normal'},
            {'pane': 'p1', 'socket': broken, 'run_id': 'r1', 'reason': 'normal'},
        ):
            with self.subTest(close_bad=close_kwargs):
                self.assertEqual(self.status(self.store.close_session, **close_kwargs), 400)

        for list_kwargs in (
            {'cli': 'gemini'},
            {'run_id': ''},
            {'since': '2026-11-23T00:00:00'},  # timezone 無し
            {'until': 'not-a-date'},
            {'since': '2026-11-24T00:00:00Z', 'until': '2026-11-23T00:00:00Z'},
        ):
            with self.subTest(list_bad=list_kwargs):
                self.assertEqual(self.status(self.store.sessions, **list_kwargs), 400)

    def test_CLI_の_open_close_list_とエラー形式(self):
        opened = self.cli(
            'session', 'open',
            '--run-id', 'run-1', '--role', 'frontdesk', '--cli', 'codex',
            '--session-id', 'uuid-1', '--pane', 'w1:p1', '--socket', '/tmp/s.sock', '--req', 'req-1',
        )
        self.assertEqual(opened.returncode, 0, opened.stderr)
        row = json.loads(opened.stdout)
        self.assertEqual((row['role'], row['cli'], row['req_id'], row['closed_at'], row['close_reason']),
                         ('frontdesk', 'codex', 'req-1', None, ''))

        # 重複 open は exit 1 + JSON error
        dup = self.cli(
            'session', 'open',
            '--run-id', 'run-1', '--role', 'frontdesk', '--cli', 'codex',
            '--session-id', 'uuid-1', '--pane', 'w1:p2', '--socket', '/tmp/s.sock',
        )
        self.assertEqual((dup.returncode, dup.stdout), (1, ''))
        self.assertTrue(json.loads(dup.stderr)['error'])

        # 不正な --reason / --role / --cli も JSON error で exit 1
        bad_reason = self.cli('session', 'close', '--session-id', 'uuid-1', '--cli', 'codex', '--reason', 'bogus')
        self.assertEqual((bad_reason.returncode, bad_reason.stdout), (1, ''))
        self.assertIn('reason', json.loads(bad_reason.stderr)['error'])

        # list
        listed = self.cli('session', 'list', '--run-id', 'run-1', '--cli', 'codex', '--open-only')
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(json.loads(listed.stdout), {'sessions': [row]})

        # close by --session-id --cli
        closed = self.cli('session', 'close', '--session-id', 'uuid-1', '--cli', 'codex', '--reason', 'normal')
        self.assertEqual(closed.returncode, 0, closed.stderr)
        closed_row = json.loads(closed.stdout)
        self.assertEqual(closed_row['close_reason'], 'normal')
        self.assertIsNotNone(closed_row['closed_at'])

        # 既閉鎖の close は exit 1 + JSON error
        again = self.cli('session', 'close', '--session-id', 'uuid-1', '--cli', 'codex', '--reason', 'normal')
        self.assertEqual((again.returncode, again.stdout), (1, ''))
        self.assertTrue(json.loads(again.stderr)['error'])

        # open-only では空、全体では 1 件
        self.assertEqual(json.loads(self.cli('session', 'list', '--open-only').stdout), {'sessions': []})
        self.assertEqual(json.loads(self.cli('session', 'list').stdout), {'sessions': [closed_row]})

        # close by --pane --socket --run-id
        opened2 = self.cli(
            'session', 'open',
            '--run-id', 'run-2', '--role', 'worker', '--cli', 'claude',
            '--session-id', 'uuid-2', '--pane', 'w2:p1', '--socket', '/tmp/w.sock',
        )
        self.assertEqual(opened2.returncode, 0, opened2.stderr)
        closed2 = self.cli(
            'session', 'close',
            '--pane', 'w2:p1', '--socket', '/tmp/w.sock', '--run-id', 'run-2', '--reason', 'replaced',
        )
        self.assertEqual(closed2.returncode, 0, closed2.stderr)
        self.assertEqual(json.loads(closed2.stdout)['close_reason'], 'replaced')

        # CLI の --since / --until と --help
        future = self.cli('session', 'list', '--since', '2099-01-01T00:00:00Z')
        self.assertEqual(future.returncode, 0, future.stderr)
        self.assertEqual(json.loads(future.stdout), {'sessions': []})
        past = self.cli('session', 'list', '--until', '2000-01-01T00:00:00Z')
        self.assertEqual(past.returncode, 0, past.stderr)
        self.assertEqual(json.loads(past.stdout), {'sessions': []})
        bad_window = self.cli('session', 'list', '--since', '2026-11-24T00:00:00Z', '--until', '2026-11-23T00:00:00Z')
        self.assertEqual((bad_window.returncode, bad_window.stdout), (1, ''))
        self.assertTrue(json.loads(bad_window.stderr)['error'])
        self.assertIn('{"sessions":', self.cli('session', 'list', '--help').stdout)

    def test_sessions_表の追加は既存表を変えず_user_version_は_2_のまま(self):
        decision = self.store.add(request_payload())
        self.store.open_session(self.open_payload())
        with contextlib.closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 2)
            columns = [row[1] for row in connection.execute('PRAGMA table_info(sessions)')]
            decisions = [row[1] for row in connection.execute('PRAGMA table_info(decisions)')]
        self.assertEqual(
            columns,
            ['id', 'run_id', 'role', 'req_id', 'pane', 'socket', 'cli', 'native_session_id', 'opened_at', 'closed_at', 'close_reason'],
        )
        self.assertEqual(
            decisions,
            ['id', 'created_at', 'payload', 'choice', 'memo', 'decided_at', 'status', 'worker', 'token', 'lease_until', 'request_key', 'reason', 'next_step', 'reported_at'],
        )
        self.assertEqual(Store(self.db).get(decision['id'])['id'], decision['id'])

    def test_旧_release_の_app_py_と既存_v2_DB_の双方向互換(self):
        shown = subprocess.run(['git', 'show', f'{OLD_RELEASE}:app.py'], cwd=ROOT, capture_output=True)
        self.assertEqual(shown.returncode, 0, f'互換試験には {OLD_RELEASE}:app.py を取得できる Git 履歴が必要です')
        old = Path(self.temp.name) / 'old_app.py'
        old.write_bytes(shown.stdout)
        legacy = Path(self.temp.name) / 'legacy.sqlite3'
        fixture = Path(self.temp.name) / 'request.json'
        fixture.write_text(json.dumps(request_payload()), encoding='utf-8')

        # 1. 旧 release に既存 v2 DB を作らせる (sessions 表なし)
        added = self.cli('add', '--file', str(fixture), script=old, db=legacy)
        self.assertEqual(added.returncode, 0, added.stderr)
        old_run = self.cli('run', 'open', '--run-id', 'r1', '--role', 'worker', '--pane', 'p1', '--socket', '/tmp/s', script=old, db=legacy)
        self.assertEqual(old_run.returncode, 0, old_run.stderr)

        # 2. 新 app.py が同じ DB に sessions 表を足して open / list / close できる
        opened = self.cli(
            'session', 'open',
            '--run-id', 'r1', '--role', 'worker', '--cli', 'codex',
            '--session-id', 'uuid-legacy', '--pane', 'p1', '--socket', '/tmp/s',
            db=legacy,
        )
        self.assertEqual(opened.returncode, 0, opened.stderr)
        listed = self.cli('session', 'list', '--run-id', 'r1', db=legacy)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(json.loads(listed.stdout), {'sessions': [json.loads(opened.stdout)]})

        # 3. 旧 release が sessions 表追加後の DB でもそのまま動く
        old_list = self.cli('list', script=old, db=legacy)
        self.assertEqual(old_list.returncode, 0, old_list.stderr)
        self.assertEqual(json.loads(old_list.stdout)['decisions'][0]['id'], json.loads(added.stdout)['id'])
        old_show = self.cli('run', 'show', '--run-id', 'r1', script=old, db=legacy)
        self.assertEqual(old_show.returncode, 0, old_show.stderr)
        self.assertEqual(len(json.loads(old_show.stdout)['runs']), 1)


if __name__ == '__main__':
    unittest.main()
