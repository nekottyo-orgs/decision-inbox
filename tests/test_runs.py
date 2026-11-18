import contextlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import AppError, Store
from test_app import isolated_env, request_payload

ROOT = Path(__file__).resolve().parents[1]
# runs 表を足す前の release (PR 13 の merge)。旧 release の app.py がそのまま動くかを確かめる基準
OLD_RELEASE = 'b4fde647'


class RunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'runs.sqlite3'
        self.store = Store(self.db)

    def status(self, call, *args):
        with self.assertRaises(AppError) as caught:
            call(*args)
        return caught.exception.status

    def cli(self, *arguments, script=None):
        return subprocess.run([sys.executable, str(script or ROOT / 'app.py'), '--db', str(self.db), *arguments], capture_output=True, text=True, timeout=10, env=isolated_env(self.temp.name))

    def run_open(self, **changes):
        return self.store.open_run({'run_id': 'r1', 'role': 'worker', 'pane': 'p1', 'socket': '/tmp/s', **changes})

    def test_開いた行は_closed_at_が空で_close_で入る(self):
        row = self.run_open()
        self.assertIsNone(row['closed_at'])
        self.assertEqual((row['run_id'], row['role'], row['pane'], row['socket']), ('r1', 'worker', 'p1', '/tmp/s'))
        closed = self.store.close_run('r1', 'worker')
        self.assertEqual(closed['opened_at'], row['opened_at'])
        self.assertIsNotNone(closed['closed_at'])

    def test_同じ_run_id_と_role_の開いた行は断り_閉じれば開き直せる(self):
        self.run_open()
        self.assertEqual(self.status(self.run_open), 409)
        self.run_open(role='reviewer', pane='p2')
        self.store.close_run('r1', 'worker')
        self.assertEqual(self.run_open(pane='p3')['pane'], 'p3')
        self.assertEqual(self.status(self.store.close_run, 'r1', 'nobody'), 404)

    def test_サロゲートを含む引数は_SQLite_に渡す前に_400_で断る(self):
        # Unix で decode できないバイトを含むパスは surrogateescape で文字列に入る
        broken = b'/tmp/\xff'.decode(errors='surrogateescape')
        self.assertEqual(self.status(lambda: self.run_open(socket=broken)), 400)
        self.assertEqual(self.status(self.store.close_run, broken, 'worker'), 400)
        self.assertEqual(self.status(self.store.runs, None, 'p1', broken), 400)
        self.assertEqual(self.run_open(pane='作業')['pane'], '作業')

    def test_show_は開いた行だけが既定で_all_で閉じた行も出す(self):
        self.run_open()
        self.store.close_run('r1', 'worker')
        self.run_open(pane='p3')
        self.assertEqual([row['pane'] for row in self.store.runs(run_id='r1')['runs']], ['p3'])
        self.assertEqual([row['pane'] for row in self.store.runs(run_id='r1', include_closed=True)['runs']], ['p1', 'p3'])
        self.assertEqual([row['run_id'] for row in self.store.runs(pane='p3', socket='/tmp/s')['runs']], ['r1'])
        self.assertEqual(self.store.runs(pane='p1', socket='/tmp/s')['runs'], [])
        self.assertEqual(self.store.runs(run_id='none')['runs'], [])

    def test_入力の検査(self):
        self.assertEqual(self.status(lambda: self.run_open(role=' ')), 400)
        self.assertEqual(self.status(lambda: self.run_open(pane='x' * 201)), 400)
        self.assertEqual(self.status(self.store.open_run, {'run_id': 'r', 'role': 'w', 'pane': 'p'}), 400)
        self.assertEqual(self.status(self.store.runs), 400)
        self.assertEqual(self.status(lambda: self.store.runs(pane='p1')), 400)
        self.assertEqual(self.status(lambda: self.store.runs(run_id='r1', pane='p1', socket='s')), 400)

    def test_CLI_の_open_show_close(self):
        opened = self.cli('run', 'open', '--run-id', 'r1', '--role', 'worker', '--pane', 'p1', '--socket', '/tmp/s')
        self.assertEqual(opened.returncode, 0, opened.stderr)
        self.assertIsNone(json.loads(opened.stdout)['closed_at'])
        again = self.cli('run', 'open', '--run-id', 'r1', '--role', 'worker', '--pane', 'p2', '--socket', '/tmp/s')
        self.assertEqual((again.returncode, again.stdout), (1, ''))
        self.assertEqual(json.loads(self.cli('run', 'show', '--run-id', 'r1').stdout)['runs'][0]['pane'], 'p1')
        self.assertEqual(json.loads(self.cli('run', 'show', '--pane', 'p1', '--socket', '/tmp/s').stdout)['runs'][0]['run_id'], 'r1')
        self.assertIsNotNone(json.loads(self.cli('run', 'close', '--run-id', 'r1', '--role', 'worker').stdout)['closed_at'])
        self.assertEqual(json.loads(self.cli('run', 'show', '--run-id', 'r1').stdout), {'runs': []})
        self.assertEqual(len(json.loads(self.cli('run', 'show', '--run-id', 'r1', '--all').stdout)['runs']), 1)
        self.assertEqual(self.cli('run', 'show').returncode, 1)

    def test_runs_表の追加は既存表を変えず_user_version_は_2_のまま(self):
        decision = self.store.add(request_payload())
        self.run_open()
        with contextlib.closing(sqlite3.connect(self.db)) as connection, connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 2)
            columns = [row[1] for row in connection.execute('PRAGMA table_info(runs)')]
            decisions = [row[1] for row in connection.execute('PRAGMA table_info(decisions)')]
        self.assertEqual(columns, ['id', 'run_id', 'role', 'pane', 'socket', 'opened_at', 'closed_at'])
        self.assertEqual(decisions, ['id', 'created_at', 'payload', 'choice', 'memo', 'decided_at', 'status', 'worker', 'token', 'lease_until', 'request_key', 'reason', 'next_step', 'reported_at'])
        self.assertEqual(Store(self.db).get(decision['id'])['id'], decision['id'])

    def test_旧_release_の_app_py_が移行後の_DB_でもそのまま動く(self):
        shown = subprocess.run(['git', 'show', f'{OLD_RELEASE}:app.py'], cwd=ROOT, capture_output=True)
        if shown.returncode != 0:
            self.skipTest('旧 release の app.py を git から取り出せない')
        old = Path(self.temp.name) / 'old_app.py'
        old.write_bytes(shown.stdout)
        decision = self.store.add(request_payload())
        self.run_open()
        listed = self.cli('list', script=old)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(json.loads(listed.stdout)['decisions'][0]['id'], decision['id'])
        answered = self.cli('task', 'add', '--id', 't1', '--title', '題', script=old)
        self.assertEqual(answered.returncode, 0, answered.stderr)
        self.assertEqual(self.cli('run', 'show', '--run-id', 'r1').returncode, 0)


if __name__ == '__main__':
    unittest.main()
