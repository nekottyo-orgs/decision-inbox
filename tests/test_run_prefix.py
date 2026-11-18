import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import Store
from test_app import isolated_env, request_payload

ROOT = Path(__file__).resolve().parents[1]
SOCKET = '/tmp/run-prefix-test.sock'


class RunPrefixTests(unittest.TestCase):
    # 実 DB には触れず、一時ディレクトリの DB と pre-add 無しの env で CLI を起動する
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'prefix.sqlite3'
        self.store = Store(self.db)
        self.store.open_run({'run_id': 'r1', 'role': 'orchestrator', 'pane': 'p1', 'socket': SOCKET})

    def add(self, pane='p1', socket=SOCKET, **changes):
        path = self.root / 'request.json'
        path.write_text(json.dumps(request_payload(**changes)), encoding='utf-8')
        env = {**isolated_env(self.root), 'HERDR_SOCKET_PATH': socket}
        env.pop('HERDR_PANE_ID', None)
        if pane is not None:
            env['HERDR_PANE_ID'] = pane
        return subprocess.run([sys.executable, str(ROOT / 'app.py'), '--db', str(self.db), 'add', '--file', str(path)], capture_output=True, text=True, timeout=10, env=env)

    def test_run_の_pane_で接頭辞が無い登録は断る(self):
        for changes in ({}, {'request_key': 'other/x'}, {'request_key': 'frontdesk-r2/x'}, {'request_key': 'frontdesk-r1/'}, {'request_key': 'frontdesk/20261119-x'}):
            result = self.add(**changes)
            self.assertEqual(result.returncode, 1, changes)
            self.assertIn('frontdesk-r1/', result.stderr)
        self.assertEqual(self.store.list(), [])

    def test_run_の接頭辞付きは通る(self):
        result = self.add(request_key='frontdesk-r1/20261119-x')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_複数の_run_を持つ_pane_はどれかの接頭辞なら通る(self):
        self.store.open_run({'run_id': 'r2', 'role': 'worker', 'pane': 'p1', 'socket': SOCKET})
        self.assertEqual(self.add(request_key='frontdesk-r2/x').returncode, 0)
        self.assertEqual(self.add(request_key='frontdesk-r3/x').returncode, 1)

    def test_run_を持たない_pane_と_env_無しと別_socket_は通す(self):
        self.assertEqual(self.add(pane='p9', request_key='free-key').returncode, 0)
        self.assertEqual(self.add(pane=None).returncode, 0)
        self.assertEqual(self.add(socket='/tmp/other.sock', request_key='free-key-2').returncode, 0)

    def test_閉じた_run_の_pane_は通す(self):
        self.store.close_run('r1', 'orchestrator')
        self.assertEqual(self.add(request_key='free-key').returncode, 0)

    def test_HTTP_の登録は_pane_を見ない(self):
        self.assertEqual(self.store.add(request_payload(request_key='free-key'))['request_key'], 'free-key')


if __name__ == '__main__':
    unittest.main()
