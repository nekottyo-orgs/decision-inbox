import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
        renamed = self.store.move_message(self.payload(state='done', req='k/2'))
        self.assertEqual(renamed['req'], 'k/2')

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
        # 呼ぶ側 (run-inbox.sh) は stderr の先頭行を警告に出すので、error の JSON も契約に含める
        for arguments in (('move', '--msg', NAME, '--state', 'held'), ('move', '--msg', NAME, '--state', 'gone'), ('list', '--state', 'gone')):
            with self.subTest(arguments=arguments):
                bad = self.cli('msg', arguments[0], '--run-id', 'r1', *arguments[1:])
                self.assertEqual(bad.returncode, 1, bad.stderr)
                self.assertTrue(json.loads(bad.stderr)['error'])

    def inbox(self):
        inbox = Path(self.temp.name) / 'inbox'
        inbox.mkdir()
        for box in ('new', 'hold', 'cur'):
            (inbox / box).mkdir()
        return inbox

    def test_sync_で_new_held_done_を反映し_別_run_と既存_done_は保つ(self):
        inbox = self.inbox()
        message = inbox / 'new' / NAME
        message.write_text('req: task/1\n\n本文\n', encoding='utf-8')
        (inbox / 'cur' / OTHER).write_text('本文', encoding='utf-8')
        other_run = self.store.move_message(self.payload(run_id='r2'))
        initial = self.cli('msg', 'sync', '--run-id', 'r1', '--inbox', str(inbox))
        self.assertEqual(initial.returncode, 0, initial.stderr)
        self.assertEqual(json.loads(initial.stdout), {'targets': 1, 'done': 0})
        first, = self.store.messages('r1')['messages']
        self.assertEqual((first['state'], first['req']), ('new', 'task/1'))
        self.assertEqual(self.store.sync_messages('r1', inbox), {'targets': 1, 'done': 0})
        self.assertEqual(self.store.messages('r1')['messages'], [first])

        held = inbox / 'hold' / NAME
        wait = inbox / 'hold' / (NAME + '.wait')
        wait.write_text('msg=task/2\n', encoding='ascii')
        message.rename(held)
        self.assertEqual(self.store.sync_messages('r1', inbox), {'targets': 1, 'done': 0})
        row, = self.store.messages('r1')['messages']
        self.assertEqual((row['id'], row['state'], row['wait'], row['received_at']), (first['id'], 'held', 'msg=task/2', first['received_at']))
        wait.write_text('msg=task/3\n', encoding='ascii')
        self.store.sync_messages('r1', inbox)
        self.assertEqual(self.store.messages('r1')['messages'][0]['wait'], 'msg=task/3')

        held.rename(inbox / 'cur' / NAME)
        self.assertEqual(self.store.sync_messages('r1', inbox), {'targets': 0, 'done': 1})
        done, = self.store.messages('r1')['messages']
        self.assertEqual((done['id'], done['state'], done['wait'], done['req']), (first['id'], 'done', None, 'task/1'))
        self.assertEqual(self.store.sync_messages('r1', inbox), {'targets': 0, 'done': 0})
        self.assertEqual(self.store.messages('r1')['messages'], [done])
        self.assertEqual(self.store.messages('r2')['messages'], [other_run])

    def test_sync_は不正な待ち先や置き場の欠損で行を更新しない(self):
        inbox = self.inbox()
        existing = self.store.move_message(self.payload())
        (inbox / 'new' / OTHER).write_text('\nreq: 本文\n', encoding='utf-8')
        held = inbox / 'hold' / NAME
        held.touch()
        wait = inbox / 'hold' / (NAME + '.wait')
        wait.write_text('decision=short\n', encoding='ascii')
        bad = self.cli('msg', 'sync', '--run-id', 'r1', '--inbox', str(inbox))
        self.assertEqual(bad.returncode, 1, bad.stderr)
        self.assertIn(NAME, json.loads(bad.stderr)['error'])
        held.unlink()
        wait.unlink()
        (inbox / 'hold').rmdir()
        self.assertEqual(self.status(self.store.sync_messages, 'r1', inbox), 400)
        self.assertEqual(self.store.messages('r1')['messages'], [existing])
        (inbox / 'hold').mkdir()
        self.store.sync_messages('r1', inbox)
        done, new = self.store.messages('r1')['messages']
        self.assertEqual((done['state'], new['state'], new['req']), ('done', 'new', None))

    def test_sync_は個別の_msg_move_より長い既存の待ち先も反映する(self):
        inbox = self.inbox()
        wait = 'msg=' + 'x' * 1024
        (inbox / 'hold' / NAME).touch()
        (inbox / 'hold' / (NAME + '.wait')).write_text(wait + '\n', encoding='ascii')
        (inbox / 'new' / OTHER).touch()

        self.assertEqual(self.store.sync_messages('r1', inbox), {'targets': 2, 'done': 0})
        rows = {row['msg']: row for row in self.store.messages('r1')['messages']}
        self.assertEqual((rows[NAME]['state'], rows[NAME]['wait']), ('held', wait))
        self.assertEqual(rows[OTHER]['state'], 'new')
        for subcommand in ('import', 'move'):
            result = self.cli('msg', subcommand, '--run-id', 'r1', '--msg', NAME, '--state', 'held', '--wait', wait)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn('wait', json.loads(result.stderr)['error'])

    def test_sync_は走査中の移動を検出して終端化しない(self):
        inbox = self.inbox()
        existing = self.store.move_message(self.payload())
        message = inbox / 'new' / NAME
        message.touch()

        def move_during_scan(_handle):
            message.rename(inbox / 'cur' / NAME)

        with patch('app.message_request', side_effect=move_during_scan):
            self.assertEqual(self.status(self.store.sync_messages, 'r1', inbox), 409)
        self.assertEqual(self.store.messages('r1')['messages'], [existing])

    def test_旧_release_の_app_py_が_messages_表のある_DB_でも動く(self):
        # skip にすると互換の守りが無いことに気づけないので、取り出せなければ失敗にする
        shown = subprocess.run(['git', 'show', f'{OLD_RELEASE}:app.py'], cwd=ROOT, capture_output=True)
        self.assertEqual(shown.returncode, 0, '旧 release の app.py を git から取り出せない')
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
