import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import Store
from test_app import isolated_env, request_payload

ROOT = Path(__file__).resolve().parents[1]


class CliHookAndTokenFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'data' / 'cli.sqlite3'
        self.hooks = self.root / 'hooks'
        self.claims = self.root / 'claims'
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

    def answered(self, **changes):
        item = self.store.add(request_payload(**changes))
        return self.store.answer(item['id'], {'choice': 'minimal'})

    def test_hooks_は_pre_add_を出し_DB_を作らない(self):
        missing = self.root / 'none' / 'x.sqlite3'
        result = subprocess.run([sys.executable, str(ROOT / 'app.py'), '--db', str(missing), 'hooks'], capture_output=True, text=True, timeout=10, env={**os.environ, 'DECISION_INBOX_HOOKS_DIR': str(self.hooks), 'DECISION_INBOX_CLAIMS_DIR': ''})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'pre_add': None})
        self.assertFalse(missing.parent.exists())
        self.hook('#!/bin/sh\nexit 0\n', mode=0o644)
        self.assertIsNone(self.ok('hooks')['pre_add'])
        path = self.hook('#!/bin/sh\nexit 0\n')
        self.assertEqual(self.ok('hooks'), {'pre_add': str(path)})

    def test_pre_add_は_exit_0_で登録し非0_で断る(self):
        seen = self.root / 'seen.json'
        self.hook(f'#!/bin/sh\ncat > {seen}\n[ "$HOOK_DECIDES" = ok ] && exit 0\nprintf "%0600d" 0 >&2\nexit 10\n')
        refused = self.cli('add', '--file', str(self.payload_file(request_key='run-a/1')))
        self.assertEqual(refused.returncode, 1)
        error = json.loads(refused.stderr)['error']
        self.assertEqual(error, 'pre-add が登録を断りました: ' + '0' * 500)
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
        self.hook(f'#!/bin/sh\ntouch {called}\n')
        result = self.cli('add', '--file', str(self.payload_file(title='')))
        self.assertEqual(result.returncode, 1)
        self.assertFalse(called.exists())

    def test_claim_token_file_auto_で保存し_complete_で消す(self):
        item = self.answered()
        claimed = self.cli('claim', '--worker', 'w', '--token-file', 'auto')
        self.assertEqual(claimed.returncode, 0, claimed.stderr)
        result = json.loads(claimed.stdout)
        with self.store.db() as connection:
            token = connection.execute('SELECT token FROM decisions WHERE id=?', (item['id'],)).fetchone()[0]
        self.assertNotIn('token', result)
        self.assertNotIn(token, claimed.stdout + claimed.stderr)
        path = Path(result['token_file'])
        self.assertEqual(path.parent, self.claims)
        self.assertRegex(path.name, item['id'] + r'-[0-9a-f]{16}\.json$')
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.claims.stat().st_mode), 0o700)
        self.assertEqual(json.loads(path.read_text()), {'id': item['id'], 'token': token, 'worker': 'w', 'expires_at': result['expires_at']})
        self.assertEqual(list(self.claims.iterdir()), [path])
        listed = self.cli('claims', '--id', item['id'])
        self.assertNotIn(token, listed.stdout)
        self.assertEqual(json.loads(listed.stdout), {'claims': [{'id': item['id'], 'token_file': str(path), 'worker': 'w', 'expires_at': result['expires_at'], 'active': True}]})
        renewed = self.ok('renew', '--id', item['id'], '--token-file', str(path), '--lease-seconds', '3600')
        self.assertEqual(json.loads(path.read_text())['expires_at'], renewed['expires_at'])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        other = self.answered(title='別件')
        wrong = self.cli('complete', '--id', other['id'], '--token-file', str(path))
        self.assertEqual(wrong.returncode, 1)
        self.assertTrue(path.exists())
        self.assertEqual(self.ok('complete', '--id', item['id'], '--token-file', str(path))['answer']['status'], 'completed')
        self.assertFalse(path.exists())

    def test_report_は成功したらファイルを消し_token_と排他(self):
        item = self.answered()
        target = self.root / 'mine.json'
        self.assertEqual(self.ok('claim', '--worker', 'w', '--token-file', str(target))['token_file'], str(target))
        both = self.cli('report', '--id', item['id'], '--token', 'x', '--token-file', str(target), '--status', 'blocked', '--reason', 'r', '--next-step', 'n')
        self.assertEqual(both.returncode, 2)
        neither = self.cli('complete', '--id', item['id'])
        self.assertEqual(neither.returncode, 2)
        self.assertEqual(self.ok('report', '--id', item['id'], '--token-file', str(target), '--status', 'blocked', '--reason', 'r', '--next-step', 'n')['answer']['status'], 'blocked')
        self.assertFalse(target.exists())

    def test_再_claim_は別ファイルになり旧ファイルは無効(self):
        item = self.answered()
        first = self.ok('claim', '--worker', 'w', '--lease-seconds', '1', '--token-file', 'auto')
        time.sleep(1.1)
        second = self.ok('claim', '--worker', 'w', '--token-file', 'auto')
        self.assertEqual(second['decision']['id'], item['id'])
        self.assertNotEqual(first['token_file'], second['token_file'])
        stale = self.cli('complete', '--id', item['id'], '--token-file', first['token_file'])
        self.assertEqual(stale.returncode, 1)
        actives = {claim['token_file']: claim['active'] for claim in self.ok('claims')['claims']}
        self.assertEqual(actives, {first['token_file']: False, second['token_file']: True})

    def test_対象なしと失敗では_claim_もファイルも残さない(self):
        self.assertEqual(self.ok('claim', '--worker', 'w', '--token-file', 'auto'), {'decision': None})
        self.assertEqual(list(self.claims.iterdir()), [])
        item = self.answered()
        unopenable = self.cli('claim', '--worker', 'w', '--token-file', str(self.root / 'no-dir' / 'x.json'))
        self.assertEqual(unopenable.returncode, 1)
        self.assertEqual(self.store.get(item['id'])['answer']['status'], 'pending')
        occupied = self.root / 'occupied.json'
        occupied.write_text('{}')
        refused = self.cli('claim', '--worker', 'w', '--token-file', str(occupied))
        self.assertEqual(refused.returncode, 1)
        self.assertEqual(occupied.read_text(), '{}')
        self.assertEqual(self.store.get(item['id'])['answer']['status'], 'pending')
        self.assertEqual([path.name for path in self.root.iterdir() if path.name.endswith('.tmp')], [])
        empty = self.cli('claim', '--worker', 'w', '--token-file', '')
        self.assertEqual((empty.returncode, empty.stdout), (1, ''))
        self.assertEqual(self.store.get(item['id'])['answer']['status'], 'pending')
        legacy = self.ok('claim', '--worker', 'w')
        self.assertEqual(legacy['decision']['id'], item['id'])
        self.assertIn('token', legacy)

    def test_lease_期限から_24_時間過ぎたファイルを_claim_で消す(self):
        self.claims.mkdir(mode=0o700)
        old = self.claims / 'old.json'
        fresh = self.claims / 'fresh.json'
        old.write_text(json.dumps({'id': 'a', 'token': 't', 'worker': 'w', 'expires_at': '2000-01-01T00:00:00+00:00'}))
        fresh.write_text(json.dumps({'id': 'b', 'token': 't', 'worker': 'w', 'expires_at': '2999-01-01T00:00:00+00:00'}))
        self.ok('claim', '--worker', 'w', '--token-file', 'auto')
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())


if __name__ == '__main__':
    unittest.main()
