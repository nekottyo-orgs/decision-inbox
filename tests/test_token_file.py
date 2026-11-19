import contextlib
import io
import json
import os
from pathlib import Path
import stat
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
from app import AppError, Store
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
        self.hook(f'#!/bin/sh\ncat > {shlex.quote(str(seen))}\n[ "$HOOK_DECIDES" = ok ] && exit 0\nprintf "%0600d" 0 >&2\nexit 10\n')
        refused = self.cli('add', '--file', str(self.payload_file(request_key='run-a/1')), env={'HOOK_DECIDES': 'reject'})
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
        self.hook(f'#!/bin/sh\ntouch {shlex.quote(str(called))}\n')
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
        # token は有効なまま id だけを変える。Store.active ではなく --id との照合で断ることを見る
        valid = path.read_text()
        path.write_text(json.dumps({**json.loads(valid), 'id': 'f' * 32}))
        wrong = self.cli('complete', '--id', item['id'], '--token-file', str(path))
        self.assertEqual(wrong.returncode, 1)
        self.assertEqual(json.loads(wrong.stderr)['error'], 'token ファイルの id が --id と一致しません')
        self.assertTrue(path.exists())
        path.write_text(valid)
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

    def read_only_token_file(self):
        # token ファイルの親を書き込み不可にして unlink だけを失敗させる (root は権限を無視するので試験できない)
        if os.geteuid() == 0:
            self.skipTest('root は権限で止まらない')
        item = self.answered()
        target = self.root / 'locked' / 'mine.json'
        target.parent.mkdir()
        self.ok('claim', '--worker', 'w', '--token-file', str(target))
        target.parent.chmod(0o500)
        self.addCleanup(target.parent.chmod, 0o700)
        return item, target

    def test_complete_は_token_ファイルを消せなくても_DB_の完了を成功として返す(self):
        item, target = self.read_only_token_file()
        result = self.cli('complete', '--id', item['id'], '--token-file', str(target))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['answer']['status'], 'completed')
        self.assertIn('warning', json.loads(result.stderr))
        self.assertTrue(target.exists())

    def test_report_は_token_ファイルを消せなくても_DB_の報告を成功として返す(self):
        item, target = self.read_only_token_file()
        result = self.cli('report', '--id', item['id'], '--token-file', str(target), '--status', 'blocked', '--reason', 'r', '--next-step', 'n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['answer']['status'], 'blocked')
        self.assertIn('warning', json.loads(result.stderr))

    def test_commit_の直後に落ちても_token_入りの一時ファイルは残らない(self):
        # finally の後始末が走らない落ち方 (SIGKILL) を、後始末を空にして近似する
        self.answered()
        with mock.patch.object(app, 'unlink_temp', lambda path: None):
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.dict(os.environ, {'DECISION_INBOX_CLAIMS_DIR': str(self.claims)}), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = app.main(['--db', str(self.db), 'claim', '--worker', 'w', '--token-file', 'auto'])
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual([p.name for p in self.claims.iterdir() if p.name.endswith('.tmp')], [])

    def test_再_claim_は別ファイルになり旧ファイルは無効(self):
        item = self.answered()
        first = self.ok('claim', '--worker', 'w', '--token-file', 'auto')
        # sleep でなく DB の lease を過去にする。ファイルの expires_at は先のままなので、claims が DB で判定するかも見られる
        self.expire_lease(item['id'], time.time() - 1)
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

    def expire_lease(self, identifier, until):
        with self.store.db() as connection:
            connection.execute('UPDATE decisions SET lease_until=? WHERE id=?', (until, identifier))

    def claim_file(self, item, expires_at, token='t', name=None):
        path = self.claims / (name or f"{item['id']}-{'0' * 16}.json")
        path.write_text(json.dumps({'id': item['id'], 'token': token, 'worker': 'w', 'expires_at': expires_at}))
        return path

    def test_lease_期限から_24_時間過ぎたファイルを_claim_で消す(self):
        self.claims.mkdir(mode=0o700)
        old = self.claim_file(self.answered(title='古い'), '2000-01-01T00:00:00+00:00')
        fresh = self.claim_file(self.answered(title='新しい'), '2999-01-01T00:00:00+00:00')
        self.ok('claim', '--worker', 'w', '--token-file', 'auto')
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())

    def test_掃除は管理外のファイルと別_DB_の判断のファイルを消さない(self):
        self.claims.mkdir(mode=0o700)
        stale = '2000-01-01T00:00:00+00:00'
        item = self.answered(title='管理外')
        foreign = self.claims / f"{'f' * 32}-{'0' * 16}.json"
        foreign.write_text(json.dumps({'id': 'f' * 32, 'token': 't', 'worker': 'w', 'expires_at': stale}))
        unmanaged = self.claim_file(item, stale, name='job-state.json')
        partial = self.claims / f"{item['id']}-{'1' * 16}.json"
        partial.write_text(json.dumps({'id': item['id'], 'expires_at': stale}))
        self.ok('claim', '--worker', 'w', '--token-file', 'auto')
        self.assertTrue(foreign.exists())
        self.assertTrue(unmanaged.exists())
        self.assertTrue(partial.exists())

    def test_token_で延長した有効な_claim_のファイルは期限が古くても消さない(self):
        item = self.answered()
        claimed = self.ok('claim', '--worker', 'w', '--lease-seconds', '3600', '--token-file', 'auto')
        path = Path(claimed['token_file'])
        data = json.loads(path.read_text())
        # --token や HTTP の renew はファイルを書き換えないので、ファイルの expires_at だけが古くなる
        path.write_text(json.dumps({**data, 'expires_at': '2000-01-01T00:00:00+00:00'}))
        self.answered(title='次')
        self.ok('claim', '--worker', 'w2', '--token-file', 'auto')
        self.assertTrue(path.exists())
        self.assertTrue(self.store.holder(item['id'], data['token']))

    def test_DB_の_lease_が切れても_24_時間はファイルを残す(self):
        item = self.answered()
        path = Path(self.ok('claim', '--worker', 'w', '--token-file', 'auto')['token_file'])
        path.write_text(json.dumps({**json.loads(path.read_text()), 'expires_at': '2000-01-01T00:00:00+00:00'}))
        # ファイルの期限は古く、DB の lease は 1 時間前に切れた。遅いほうから 24 時間なので残り 23 時間は消さない
        self.expire_lease(item['id'], time.time() - 3600)
        self.ok('claim', '--worker', 'w2', '--token-file', 'auto')
        self.assertTrue(path.exists())

    def test_claims_dir_の_FIFO_で止まらない(self):
        self.claims.mkdir(mode=0o700)
        os.mkfifo(self.claims / f"{'a' * 32}-{'0' * 16}.json")
        self.assertEqual(self.ok('claims'), {'claims': []})
        self.answered()
        self.assertIsNotNone(self.ok('claim', '--worker', 'w', '--token-file', 'auto')['decision'])

    def test_上限を超える_token_ファイルは中身が正しくても断る(self):
        item = self.answered()
        target = self.root / 'mine.json'
        self.ok('claim', '--worker', 'w', '--token-file', str(target))
        target.write_text(target.read_text() + ' ' * 65536)
        result = self.cli('complete', '--id', item['id'], '--token-file', str(target))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.store.get(item['id'])['answer']['status'], 'processing')


class InProcessTokenFileTests(unittest.TestCase):
    """mock が要る検査。main を同じ process で呼ぶので env と --db を一時ディレクトリに閉じる"""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = self.root / 'data' / 'cli.sqlite3'
        self.claims = self.root / 'claims'
        patcher = mock.patch.dict(os.environ, isolated_env(self.root))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = Store(self.db)
        item = self.store.add(request_payload())
        self.item = self.store.answer(item['id'], {'choice': 'minimal'})

    def main(self, *arguments):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = app.main(['--db', str(self.db), *arguments])
        self.assertEqual(code, 0, err.getvalue())
        return json.loads(out.getvalue())

    def locked(self):
        # 他の process が書き込みロックを握っているかを、待たずに BEGIN IMMEDIATE を試して見る
        connection = sqlite3.connect(self.db, timeout=0, isolation_level=None)
        try:
            connection.execute('BEGIN IMMEDIATE')
        except sqlite3.OperationalError as error:
            # ロック待ち (SQLITE_BUSY) 以外の失敗 (I/O エラー等) を「ロックを握っている」と読まない。
            # sqlite_errorcode は Python 3.11 からなので、どの Python の version でも同じ SQLite の文言で見分ける
            if str(error) != 'database is locked':
                raise
            return True
        finally:
            connection.close()
        return False

    def test_renew_のファイル置換は_DB_の書き込みロックの中で行う(self):
        target = self.root / 'mine.json'
        self.main('claim', '--worker', 'w', '--token-file', str(target))
        seen = []
        replace = os.replace

        def spy(source, destination):
            seen.append(self.locked())
            return replace(source, destination)
        with mock.patch.object(app.os, 'replace', spy):
            self.main('renew', '--id', self.item['id'], '--token-file', str(target), '--lease-seconds', '3600')
        # ロックの外で置き換えると、complete と別 claim が同じ PATH を使った後に古い token で上書きできる
        self.assertEqual(seen, [True])

    def test_renew_は別の_claim_に置き換わったファイルを上書きしない(self):
        target = self.root / 'mine.json'
        self.main('claim', '--worker', 'w', '--token-file', str(target))
        original = json.loads(target.read_text())
        read = app.read_token_file
        calls = []

        def swap(path, identifier):
            data = read(path, identifier)
            calls.append(path)
            if len(calls) == 1:
                # 最初の読み取りの後、別の claim が同じ PATH に入った状態を作る
                # 書き換えでなく別ファイルからの置き換えにする。ファイルの同一性で見張る実装でも検出できるように
                replacement = target.with_name('replacement.json')
                replacement.write_text(json.dumps({**original, 'token': 'other'}))
                os.replace(replacement, target)
            return data
        with mock.patch.object(app, 'read_token_file', swap), contextlib.redirect_stderr(io.StringIO()):
            code = app.main(['--db', str(self.db), 'renew', '--id', self.item['id'], '--token-file', str(target)])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(target.read_text())['token'], 'other')

    def test_claim_は_commit_前に公開先ディレクトリを_fsync_する(self):
        directories = []
        fsync = os.fsync

        def spy(descriptor):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                directories.append((os.fstat(descriptor).st_ino, self.locked()))
            return fsync(descriptor)
        with mock.patch.object(app.os, 'fsync', spy):
            self.main('claim', '--worker', 'w', '--token-file', 'auto')
        # auto が作った claims_dir と、その名前を持つ親の両方
        self.assertEqual(sorted(directories), sorted([(self.claims.stat().st_ino, True), (self.root.stat().st_ino, True)]))

    def test_既にある_claims_dir_でも親を_fsync_する(self):
        # 対象なしの claim でも claims_dir は作られる。その後の claim で親の名前が永続化されないままにしない
        self.claims.mkdir(mode=0o700)
        directories = []
        fsync = os.fsync

        def spy(descriptor):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                directories.append((os.fstat(descriptor).st_ino, self.locked()))
            return fsync(descriptor)
        with mock.patch.object(app.os, 'fsync', spy):
            self.main('claim', '--worker', 'w', '--token-file', 'auto')
        # 書き込みロックを握っている間、つまり commit の前に親を fsync している
        self.assertIn((self.root.stat().st_ino, True), directories)

    def test_claims_dir_は中間のディレクトリまでは作らない(self):
        nested = self.root / 'missing' / 'claims'
        err = io.StringIO()
        with mock.patch.dict(os.environ, {'DECISION_INBOX_CLAIMS_DIR': str(nested)}), contextlib.redirect_stderr(err):
            code = app.main(['--db', str(self.db), 'claim', '--worker', 'w', '--token-file', 'auto'])
        self.assertEqual(code, 1)
        self.assertIn('claims_dir の親ディレクトリがありません', json.loads(err.getvalue())['error'])
        self.assertFalse(nested.parent.exists())
        self.assertEqual(self.store.get(self.item['id'])['answer']['status'], 'pending')

    def test_公開先ディレクトリの_fsync_に失敗したら_claim_もファイルも残さない(self):
        fsync = os.fsync

        def failing(descriptor):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise OSError(5, 'Input/output error')
            return fsync(descriptor)
        with mock.patch.object(app.os, 'fsync', failing), contextlib.redirect_stderr(io.StringIO()):
            code = app.main(['--db', str(self.db), 'claim', '--worker', 'w', '--token-file', 'auto'])
        self.assertEqual(code, 1)
        self.assertEqual(list(self.claims.iterdir()), [])
        with self.store.db() as connection:
            row = connection.execute('SELECT status, token FROM decisions WHERE id=?', (self.item['id'],)).fetchone()
        self.assertEqual(tuple(row), ('pending', None))
        self.assertEqual(self.main('claim', '--worker', 'w', '--token-file', 'auto')['decision']['id'], self.item['id'])

    def test_一時ファイルを消せないときは警告を出し_claim_は成功させる(self):
        unlink = Path.unlink

        def failing(path, missing_ok=False):
            if path.name.startswith('.claim-'):
                raise PermissionError(13, 'Permission denied')
            return unlink(path, missing_ok=missing_ok)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(Path, 'unlink', failing), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = app.main(['--db', str(self.db), 'claim', '--worker', 'w', '--token-file', 'auto'])
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(json.loads(out.getvalue())['decision']['id'], self.item['id'])
        warning = json.loads(err.getvalue())['warning']
        self.assertTrue(warning.startswith('token を含む一時ファイルを消せませんでした: '), warning)
        self.assertNotIn(json.loads(Path(json.loads(out.getvalue())['token_file']).read_text())['token'], warning)

    def test_書き込み中に_lease_が切れた_claim_は_commit_しない(self):
        now = time.time()
        ran = []
        with mock.patch.object(app.time, 'time', return_value=now) as clock:
            def expire(result):
                # callback まで届いたことを記録し、その中で lease の期限まで時計を進める
                ran.append(result['decision']['id'])
                clock.return_value = now + 1
            with self.assertRaises(AppError) as caught:
                self.store.claim('w', 1, on_claim=expire)
        self.assertEqual(ran, [self.item['id']])
        self.assertEqual((caught.exception.status, str(caught.exception)), (409, 'token ファイルの書き込み中に lease が切れました'))
        self.assertEqual(self.store.get(self.item['id'])['answer']['status'], 'pending')

    def lease_until(self):
        with self.store.db() as connection:
            return connection.execute('SELECT lease_until FROM decisions WHERE id=?', (self.item['id'],)).fetchone()[0]

    def test_更新中に_lease_が切れた_renew_は_commit_しない(self):
        now = time.time()
        with mock.patch.object(app.time, 'time', return_value=now) as clock:
            claimed = self.store.claim('w', 300)

            def expire(result):
                # 300 秒から 600 秒への本当の延長を試み、callback の中で新しい期限まで時計を進める
                clock.return_value = now + 600
            with self.assertRaises(AppError) as caught:
                self.store.renew(self.item['id'], {'token': claimed['token'], 'lease_seconds': 600}, on_renew=expire)
        self.assertEqual((caught.exception.status, str(caught.exception)), (409, 'token ファイルの更新中に lease が切れました'))
        self.assertEqual(self.lease_until(), now + 300)

    def test_commit_の後に_lease_が切れた_renew_は保存したまま_409_を返す(self):
        now = time.time()
        with mock.patch.object(app.time, 'time', return_value=now) as clock:
            claimed = self.store.claim('w', 300)
            # active の確認では now、commit の後の確認では新しい期限を過ぎた時刻を返す (commit の待ちで切れた状態)
            clock.side_effect = [now, now + 700]
            with self.assertRaises(AppError) as caught:
                self.store.renew(self.item['id'], {'token': claimed['token'], 'lease_seconds': 600})
        self.assertEqual((caught.exception.status, str(caught.exception)), (409, '延長は保存しましたが lease は既に切れています。この token は使えません'))
        self.assertEqual(self.lease_until(), now + 600)

    def test_一時ファイルの警告は_stderr_が無くても_stdout_を汚さない(self):
        temp = mock.Mock()
        temp.unlink.side_effect = PermissionError(13, 'Permission denied')
        out = io.StringIO()
        with mock.patch.object(app.sys, 'stderr', None), contextlib.redirect_stdout(out):
            app.unlink_temp(temp)
        self.assertEqual(out.getvalue(), '')
        broken = mock.Mock()
        broken.write.side_effect = OSError(5, 'Input/output error')
        with mock.patch.object(app.sys, 'stderr', broken):
            app.unlink_temp(temp)


if __name__ == '__main__':
    unittest.main()
