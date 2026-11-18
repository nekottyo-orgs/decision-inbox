import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
from app import AppError, Store
from test_app import isolated_env, request_payload

ROOT = Path(__file__).resolve().parents[1]


class CliPreAddTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pre add ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'data' / 'cli.sqlite3'
        self.hooks = self.root / 'hooks'
        self.store = Store(self.db)

    def cli(self, *arguments, env=None):
        return subprocess.run([sys.executable, str(ROOT / 'app.py'), '--db', str(self.db), *arguments], capture_output=True, text=True, timeout=10, env={**isolated_env(self.root), **(env or {})})

    def ok(self, *arguments, env=None):
        result = self.cli(*arguments, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def hook(self, body, mode=0o755):
        self.hooks.mkdir(exist_ok=True)
        path = self.hooks / 'pre-add'
        path.write_text(body, encoding='utf-8')
        path.chmod(mode)
        return path

    def payload_file(self, **changes):
        path = self.root / 'request.json'
        path.write_text(json.dumps(request_payload(**changes)), encoding='utf-8')
        return path

    def test_hooks_は_pre_add_を出し_DB_を作らない(self):
        missing = self.root / 'none' / 'x.sqlite3'
        result = subprocess.run([sys.executable, str(ROOT / 'app.py'), '--db', str(missing), 'hooks'], capture_output=True, text=True, timeout=10, env=isolated_env(self.root))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'pre_add': None})
        self.assertFalse(missing.parent.exists())
        self.hook('#!/bin/sh\nexit 0\n', mode=0o644)
        self.assertIsNone(self.ok('hooks')['pre_add'])
        path = self.hook('#!/bin/sh\nexit 0\n')
        self.assertEqual(self.ok('hooks'), {'pre_add': str(path)})

    def test_pre_add_は_exit_0_で登録し非0_で断る(self):
        seen = self.root / 'seen.json'
        self.hook(f'#!/bin/sh\ncat > {shlex.quote(str(seen))}\n[ "$HOOK_DECIDES" = ok ] && exit 0\nprintf "%0600d" 0 >&2\nexit 10\n')
        refused = self.cli('add', '--file', str(self.payload_file(request_key='run-a/1')), env={'HOOK_DECIDES': 'no'})
        self.assertEqual(refused.returncode, 1)
        self.assertEqual(json.loads(refused.stderr)['error'], 'pre-add が登録を断りました: ' + '0' * 500)
        self.assertEqual(json.loads(seen.read_text())['request_key'], 'run-a/1')
        self.assertEqual(self.store.list(), [])
        added = self.ok('add', '--file', str(self.payload_file(request_key='run-a/1')), env={'HOOK_DECIDES': 'ok'})
        self.assertEqual([item['id'] for item in self.store.list()], [added['id']])

    def test_pre_add_が無い_実行できない_起動できないとき(self):
        self.ok('add', '--file', str(self.payload_file(title='hook なし')))
        self.hook('#!/bin/sh\nexit 1\n', mode=0o644)
        self.ok('add', '--file', str(self.payload_file(title='実行権なし')))
        self.hook('#!/nonexistent/interpreter\n')
        broken = self.cli('add', '--file', str(self.payload_file(title='起動失敗')))
        self.assertEqual(broken.returncode, 1)
        self.assertTrue(json.loads(broken.stderr)['error'].startswith('pre-add が登録を断りました: '))
        self.assertEqual(len(self.store.list()), 2)

    def test_不正な_payload_では_pre_add_を呼ばない(self):
        called = self.root / 'called'
        self.hook(f'#!/bin/sh\ntouch {shlex.quote(str(called))}\n')
        result = self.cli('add', '--file', str(self.payload_file(title='')))
        self.assertEqual(result.returncode, 1)
        self.assertFalse(called.exists())

    def test_update_は_pre_add_を呼ばない(self):
        item = self.store.add(request_payload())
        called = self.root / 'called'
        self.hook(f'#!/bin/sh\ntouch {shlex.quote(str(called))}\nexit 1\n')
        changes = self.root / 'changes.json'
        changes.write_text(json.dumps({'title': '更新後'}), encoding='utf-8')
        self.ok('update', '--id', item['id'], '--note', '理由', '--file', str(changes))
        self.assertFalse(called.exists())


class PreAddTimeoutTests(unittest.TestCase):
    def test_終わらない_pre_add_は打ち切って断る(self):
        with tempfile.TemporaryDirectory() as temp:
            hook = Path(temp) / 'pre-add'
            hook.write_text('#!/bin/sh\nexec sleep 5\n', encoding='utf-8')
            hook.chmod(0o755)
            with unittest.mock.patch.dict(os.environ, {'DECISION_INBOX_HOOKS_DIR': temp}), unittest.mock.patch.object(app, 'PRE_ADD_TIMEOUT', 0.2):
                with self.assertRaises(AppError) as caught:
                    app.run_pre_add(request_payload())
            self.assertIn('0.2 秒で終わりませんでした', str(caught.exception))

    def test_打ち切りでは_hook_が起こした子も止める(self):
        with tempfile.TemporaryDirectory() as temp:
            hook = Path(temp) / 'pre-add'
            pid_file = Path(temp) / 'child.pid'
            hook.write_text(f'#!/bin/sh\nsleep 5 &\necho $! > {pid_file}\nwait\n', encoding='utf-8')
            hook.chmod(0o755)
            with unittest.mock.patch.dict(os.environ, {'DECISION_INBOX_HOOKS_DIR': temp}), unittest.mock.patch.object(app, 'PRE_ADD_TIMEOUT', 0.5):
                with self.assertRaises(AppError):
                    app.run_pre_add(request_payload())
            child = int(pid_file.read_text())
            for _ in range(50):
                try:
                    os.kill(child, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.02)
            else:
                self.fail('打ち切り後も hook の子が残っている')


if __name__ == '__main__':
    unittest.main()
