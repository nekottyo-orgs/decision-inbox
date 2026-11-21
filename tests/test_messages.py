import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import AppError, Store
from test_app import isolated_env

ROOT = Path(__file__).resolve().parents[1]
# messages 表を足す前の release (task reconcile-lane --from の merge)。旧 release の app.py がそのまま動くかを確かめる基準
OLD_RELEASE = '0e7cd80'
NAME = '1-1700000000000000-pr-1:2-abc123.msg'
OTHER = '0-1700000000000001-msg-3.4-xyz.msg'


class MessageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'messages.sqlite3'
        self.store = Store(self.db)

    def status(self, call, *args):
        with self.assertRaises(AppError) as caught:
            call(*args)
        return caught.exception.status

    def cli(self, *arguments, script=None, db=None):
        return subprocess.run([sys.executable, str(script or ROOT / 'app.py'), '--db', str(db or self.db), *arguments], capture_output=True, text=True, timeout=10, env=isolated_env(self.temp.name))

    def payload(self, **changes):
        return {'run_id': 'r1', 'msg': NAME, 'state': 'new', **changes}

    def test_import_は_2_回目で_imported_false_かつ行は_1_つ(self):
        first = self.store.import_message(self.payload())
        second = self.store.import_message(self.payload(state='done'))
        self.assertTrue(first['imported'])
        self.assertFalse(second['imported'])
        self.assertEqual(second['message']['state'], 'new')
        self.assertEqual(len(self.store.messages('r1')['messages']), 1)

    def test_move_は行が無くても入り_held_から_done_で_wait_が消える(self):
        held = self.store.move_message(self.payload(state='held', wait='decision=abc', req='k/1'))
        self.assertEqual((held['state'], held['wait']), ('held', 'decision=abc'))
        done = self.store.move_message(self.payload(state='done'))
        self.assertEqual((done['state'], done['wait'], done['req']), ('done', None, 'k/1'))
        self.assertEqual(done['id'], held['id'])

    def test_tag_と_from_pane_は名前から入る(self):
        row = self.store.import_message(self.payload())['message']
        self.assertEqual((row['tag'], row['from_pane']), ('pr', '1:2'))

    def test_不正な入力は_400_で断る(self):
        broken = 'x'.encode().decode() + b'\xff'.decode(errors='surrogateescape')
        for changes in ({'msg': 'bad.msg'}, {'msg': 'x' * 201}, {'state': 'gone'}, {'state': 'held'}, {'wait': 'decision=a'},
                        {'state': 'held', 'wait': 'bad'}, {'state': 'held', 'wait': 'pr=' + 'x' * 500}, {'req': 'a b'}, {'req': 'x' * 201}, {'run_id': ''}, {'run_id': broken}):
            with self.subTest(changes=changes):
                self.assertEqual(self.status(self.store.import_message, self.payload(**changes)), 400)
                self.assertEqual(self.status(self.store.move_message, self.payload(**changes)), 400)
        self.assertEqual(self.status(self.store.messages, 'r1', 'gone'), 400)

    def test_list_は_state_で絞り込める(self):
        self.store.import_message(self.payload())
        self.store.import_message(self.payload(msg=OTHER, state='done'))
        self.store.import_message(self.payload(run_id='r2'))
        self.assertEqual([m['msg'] for m in self.store.messages('r1')['messages']], [NAME, OTHER])
        self.assertEqual([m['msg'] for m in self.store.messages('r1', 'done')['messages']], [OTHER])

    def test_CLI_の_import_move_list_が往復する(self):
        imported = self.cli('msg', 'import', '--run-id', 'r1', '--msg', NAME, '--state', 'new')
        self.assertEqual(imported.returncode, 0, imported.stderr)
        self.assertTrue(json.loads(imported.stdout)['imported'])
        moved = self.cli('msg', 'move', '--run-id', 'r1', '--msg', NAME, '--state', 'held', '--wait', 'pr=u')
        self.assertEqual(json.loads(moved.stdout)['wait'], 'pr=u')
        listed = self.cli('msg', 'list', '--run-id', 'r1', '--state', 'held')
        self.assertEqual([m['msg'] for m in json.loads(listed.stdout)['messages']], [NAME])
        bad = self.cli('msg', 'move', '--run-id', 'r1', '--msg', NAME, '--state', 'held')
        self.assertEqual(bad.returncode, 1)

    def test_旧_release_の_app_py_が_messages_表のある_DB_でも動く(self):
        try:
            shown = subprocess.run(['git', 'show', f'{OLD_RELEASE}:app.py'], cwd=ROOT, capture_output=True)
        except FileNotFoundError:
            self.skipTest('git が無い')
        if shown.returncode != 0:
            self.skipTest('旧 release の app.py を git から取り出せない')
        old = Path(self.temp.name) / 'old_app.py'
        old.write_bytes(shown.stdout)
        self.store.import_message(self.payload())
        self.cli('run', 'open', '--run-id', 'r1', '--role', 'worker', '--pane', 'p1', '--socket', '/tmp/s')
        listed = self.cli('list', script=old)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        shown_run = self.cli('run', 'show', '--run-id', 'r1', script=old)
        self.assertEqual(shown_run.returncode, 0, shown_run.stderr)
        self.assertEqual(len(json.loads(shown_run.stdout)['runs']), 1)


if __name__ == '__main__':
    unittest.main()
