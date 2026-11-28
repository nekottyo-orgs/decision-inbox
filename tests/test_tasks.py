import curses
from concurrent.futures import ThreadPoolExecutor
import json
import contextlib
import sqlite3
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import AppError, LaneConflict, Store
from unittest.mock import patch
from cue_tui import Desk, draw, focus_pane, handle_key, tmux_clients
from test_app import TASK_DEFAULTS, request_payload, seed_task
from test_tui import Screen

ROOT = Path(__file__).resolve().parents[1]
PR_URL = 'https://github.com/nekottyo/agents/pull/305'
ADD_KIND = ('--kind', 'research', '--done-when', TASK_DEFAULTS['done_when'])


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'tasks.sqlite3'
        self.store = Store(self.db)

    def status(self, call, *args):
        with self.assertRaises(AppError) as caught:
            call(*args)
        return caught.exception.status

    def test_追加した_task_は既定で_todo_になり作成と更新が同時刻になる(self):
        task = self.store.add_task({**TASK_DEFAULTS, 'id': 't1', 'title': '題'})
        self.assertEqual(self.store.tasks(), [{**task, 'decision': None}])
        self.assertEqual(task['lane'], 'todo')
        self.assertEqual(task['created_at'], task['updated_at'])
        self.assertIsNone(task['decision_id'])

    def test_同じ内容の追加は既存を返し内容違いは409(self):
        first = self.store.add_task({**TASK_DEFAULTS, 'id': 't1', 'title': '題', 'pane': 'p'})
        self.assertEqual(self.store.add_task({**TASK_DEFAULTS, 'id': 't1', 'title': '題', 'pane': 'p'}), first)
        self.assertEqual(self.status(self.store.add_task, {**TASK_DEFAULTS, 'id': 't1', 'title': '別'}), 409)

    def test_kind_と_done_when_は再送判定に入れず既存の値を返す(self):
        first = self.store.add_task({**TASK_DEFAULTS, 'id': 't1', 'title': '題'})
        again = self.store.add_task({'id': 't1', 'title': '題', 'kind': 'implement', 'done_when': PR_URL + ' が merge される'})
        self.assertEqual(again, first)
        self.assertEqual((again['kind'], again['done_when']), (TASK_DEFAULTS['kind'], TASK_DEFAULTS['done_when']))
        with contextlib.closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("INSERT INTO tasks(id, title, lane, created_at, updated_at) VALUES ('old', '旧行', 'todo', 'c', 'u')")
        legacy = self.store.add_task({'id': 'old', 'title': '旧行', 'kind': 'research', 'done_when': 'x'})
        self.assertEqual((legacy['kind'], legacy['done_when']), ('', ''))

    def test_進んだ_task_への_add_の再送は既存を返す(self):
        self.store.add_task({**TASK_DEFAULTS, 'id': 't1', 'title': '題'})
        self.store.reconcile_lane('t1', 'worker')
        moved = self.store.update_task('t1', {'remaining': '残り'})
        self.assertEqual(self.store.add_task({**TASK_DEFAULTS, 'id': 't1', 'title': '題'}), moved)

    def test_更新は指定項目だけ変え更新時刻が進む(self):
        first = self.store.add_task({**TASK_DEFAULTS, 'id': 't1', 'title': '題', 'result': 'r'})
        updated = self.store.update_task('t1', {'remaining': '残り'})
        self.assertEqual((updated['title'], updated['result'], updated['remaining']), ('題', 'r', '残り'))
        self.assertGreater(updated['updated_at'], first['updated_at'])
        self.assertEqual(updated['created_at'], first['created_at'])
        self.assertEqual(self.status(self.store.update_task, 't1', {}), 400)
        self.assertEqual(self.status(self.store.update_task, 't1', {'id': 'x'}), 400)
        self.assertEqual(self.status(self.store.update_task, 'none', {'title': 'x'}), 404)

    def test_add_と_update_は_lane_と_machine_を_payload_で受けず断る(self):
        self.store.add_task({**TASK_DEFAULTS, 'id': 't1', 'title': '題'})
        for payload in ({'lane': 'done'}, {'lane': 'x'}, {'lane': 'todo'}, {'machine': '印'}, {'lane': 'done', 'remaining': '残り'}):
            with self.assertRaises(AppError) as caught:
                self.store.update_task('t1', payload)
            self.assertEqual(caught.exception.status, 400, payload)
            self.assertNotIn('reconcile-lane', str(caught.exception))
            with self.assertRaises(AppError) as caught:
                self.store.add_task({**TASK_DEFAULTS, 'id': 't2', 'title': 'a', **payload})
            self.assertEqual(caught.exception.status, 400, payload)
            self.assertNotIn('reconcile-lane', str(caught.exception))
        tasks = self.store.tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual((tasks[0]['lane'], tasks[0]['machine'], tasks[0]['remaining']), ('todo', '', ''))

    def test_reconcile_lane_は_lane_と機械の欄を同時に書き_remaining_を変えない(self):
        self.store.add_task({**TASK_DEFAULTS, 'id': 't1', 'title': '題', 'remaining': '窓口の文章'})
        written = self.store.reconcile_lane('t1', 'human_action', '停止の疑い (機械判定 2026-11-19)')
        self.assertEqual((written['lane'], written['machine'], written['remaining']), ('human_action', '停止の疑い (機械判定 2026-11-19)', '窓口の文章'))
        self.assertEqual(len(self.store.tasks('human_action')), 1)
        cleared = self.store.reconcile_lane('t1', 'worker')
        self.assertEqual((cleared['lane'], cleared['machine'], cleared['remaining']), ('worker', '', '窓口の文章'))
        self.assertEqual(self.status(self.store.reconcile_lane, 't1', 'bogus'), 400)
        self.assertEqual(self.status(self.store.reconcile_lane, 'nope', 'done'), 404)
        self.assertEqual(self.status(self.store.reconcile_lane, 't1', 'done', 'あ' * 501), 400)

    def test_reconcile_lane_の_expect_は今の_lane_が一致するときだけ書き競合と未登録を分ける(self):
        self.store.add_task({**TASK_DEFAULTS, 'id': 't1', 'title': '題'})
        self.store.reconcile_lane('t1', 'done', '窓口')
        before = self.store.tasks()[0]
        # 窓口が done にした後、古い snapshot (worker) で todo に戻そうとする書き込み
        with self.assertRaises(LaneConflict) as caught:
            self.store.reconcile_lane('t1', 'todo', '', 'worker')
        self.assertEqual(caught.exception.status, 409)
        self.assertIn('今は done', str(caught.exception))
        self.assertEqual(self.store.tasks()[0], before)
        self.assertEqual(self.status(self.store.reconcile_lane, 'nope', 'todo', '', 'worker'), 404)
        self.assertEqual(self.status(self.store.reconcile_lane, 't1', 'todo', '', 'bogus'), 400)
        self.assertEqual(self.status(self.store.reconcile_lane, 't1', None), 400)
        moved = self.store.reconcile_lane('t1', 'todo', '', 'done')
        self.assertEqual(moved['lane'], 'todo')
        # expect を省くと従来どおり lane を確かめずに書く
        self.assertEqual(self.store.reconcile_lane('t1', 'worker')['lane'], 'worker')

    def test_machine_の無い既存の_tasks_表へ列を足し中身を残す(self):
        with contextlib.closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute('ALTER TABLE tasks DROP COLUMN machine')
            connection.execute("INSERT INTO tasks(id, title, lane, created_at, updated_at) VALUES ('old', '旧行', 'worker', 'c', 'u')")
        task = Store(self.db).tasks()[0]
        self.assertEqual((task['id'], task['lane'], task['machine']), ('old', 'worker', ''))

    def test_kind_か_done_when_が無い_空_不正値の_add_は断り_task_は増えない(self):
        base = {'id': 't1', 'title': '題', 'kind': 'research', 'done_when': '終わる'}
        missing = [{key: value for key, value in base.items() if key != gone} for gone in ('kind', 'done_when')]
        wrong = [{**base, key: value} for key, values in (('kind', ('', ' ', 'bogus', 'Research', 5, ['research'], None)), ('done_when', ('', ' ', '\n', 'あ' * 1001, 5, ['x'], None))) for value in values]
        for payload in missing + wrong:
            self.assertEqual(self.status(self.store.add_task, payload), 400, payload)
        self.assertEqual(self.store.tasks(), [])
        self.assertEqual(self.store.add_task({**base, 'done_when': 'あ' * 1000})['done_when'], 'あ' * 1000)

    def test_4_つの_kind_を保存し読み出せる(self):
        decision = self.store.add(request_payload())
        for kind in ('decision', 'monitor', 'research', 'implement'):
            self.store.add_task({'id': kind, 'title': kind, 'kind': kind, 'done_when': kind + ' が済む ' + PR_URL, 'decision_id': decision['id']})
        self.assertEqual({task['id']: (task['kind'], task['done_when']) for task in self.store.tasks()},
                         {kind: (kind, kind + ' が済む ' + PR_URL) for kind in ('decision', 'monitor', 'research', 'implement')})

    def test_implement_の_PR_が_URL_でない_add_は断り_task_は増えない(self):
        for done_when in ('PR 305 が merge される', 'PR #305 が merge される', '実装が済む', 'https://example.com/pull/1 が merge される', 'github.com/nekottyo/agents/pull/305'):
            with self.assertRaises(AppError) as caught:
                self.store.add_task({'id': 't1', 'title': '題', 'kind': 'implement', 'done_when': done_when})
            self.assertEqual(caught.exception.status, 400, done_when)
            self.assertIn('URL', str(caught.exception))
        self.assertEqual(self.store.tasks(), [])

    def test_decision_の_decision_id_が無い_add_は断り_task_は増えない(self):
        for extra in ({}, {'decision_id': ''}, {'decision_id': None}):
            with self.assertRaises(AppError) as caught:
                self.store.add_task({'id': 't1', 'title': '題', 'kind': 'decision', 'done_when': '回答が届く', **extra})
            self.assertEqual(caught.exception.status, 400, extra)
            self.assertIn('decision_id', str(caught.exception))
        self.assertEqual(self.store.tasks(), [])

    def test_PR_の_URL_と_decision_id_がそろった_add_は通る(self):
        decision = self.store.add(request_payload())
        for number in (305, 306):
            for kind in ('implement', 'decision'):
                task = self.store.add_task({'id': f'{kind}{number}', 'title': '題', 'kind': kind, 'done_when': f'https://github.com/nekottyo/agents/pull/{number} が merge される', 'decision_id': decision['id']})
                self.assertEqual((task['kind'], task['decision_id']), (kind, decision['id']))
        issue = self.store.add_task({'id': 'issue', 'title': '題', 'kind': 'implement', 'done_when': 'https://github.com/nekottyo/agents/issues/409 が close される'})
        self.assertEqual(issue['decision_id'], None)
        self.assertEqual(len(self.store.tasks()), 5)

    def test_規則より前に登録した行への再送は既存を返し_URL_の後ろが英数字なら断る(self):
        with contextlib.closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("INSERT INTO tasks(id, title, kind, done_when, lane, created_at, updated_at) VALUES ('i', '題', 'implement', '実装が済む', 'worker', 'c', 'u')")
            connection.execute("INSERT INTO tasks(id, title, kind, done_when, lane, created_at, updated_at) VALUES ('d', '題', 'decision', '回答が届く', 'worker', 'c', 'u')")
        for identifier, kind, done_when in (('i', 'implement', '実装が済む'), ('d', 'decision', '回答が届く')):
            self.assertEqual(self.store.add_task({'id': identifier, 'title': '題', 'kind': kind, 'done_when': done_when})['id'], identifier)
        # 規則を満たさない旧行でも、result などだけの update は止まらない
        self.assertEqual(self.store.update_task('i', {'result': '済み'})['result'], '済み')
        self.assertEqual(self.store.update_task('d', {'remaining': '残り'})['remaining'], '残り')
        for done_when in (PR_URL + 'abc', PR_URL + '_x'):
            self.assertEqual(self.status(self.store.add_task, {'id': 'n', 'title': '題', 'kind': 'implement', 'done_when': done_when}), 400, done_when)
        self.assertEqual(self.store.add_task({'id': 'n', 'title': '題', 'kind': 'implement', 'done_when': PR_URL + 'が merge される'})['id'], 'n')

    def test_update_も同じ規則で断り指定しない項目と_legacy_行は止めない(self):
        decision = self.store.add(request_payload())
        self.store.add_task({'id': 'i', 'title': '題', 'kind': 'implement', 'done_when': PR_URL})
        self.store.add_task({'id': 'd', 'title': '題', 'kind': 'decision', 'done_when': '回答が届く', 'decision_id': decision['id']})
        self.store.add_task({**TASK_DEFAULTS, 'id': 'r', 'title': '題'})
        for identifier, payload in (('i', {'done_when': 'PR 305 が merge される'}), ('r', {'kind': 'implement'}), ('r', {'kind': 'decision'}), ('d', {'decision_id': ''}), ('d', {'decision_id': None})):
            self.assertEqual(self.status(self.store.update_task, identifier, payload), 400, payload)
        self.assertEqual(self.store.update_task('r', {'kind': 'implement', 'done_when': PR_URL})['kind'], 'implement')
        self.assertEqual(self.store.update_task('d', {'kind': 'monitor', 'decision_id': ''})['decision_id'], None)
        self.assertEqual(self.store.update_task('i', {'result': '済み'})['result'], '済み')
        with contextlib.closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("INSERT INTO tasks(id, title, lane, created_at, updated_at) VALUES ('old', '旧行', 'worker', 'c', 'u')")
        self.assertEqual(self.store.update_task('old', {'remaining': '残り'})['remaining'], '残り')

    def test_update_で_kind_と_done_when_を変えられ空と不正値は断り値は変わらない(self):
        self.store.add_task({**TASK_DEFAULTS, 'id': 't1', 'title': '題'})
        changed = self.store.update_task('t1', {'kind': 'monitor'})
        self.assertEqual((changed['kind'], changed['done_when']), ('monitor', TASK_DEFAULTS['done_when']))
        changed = self.store.update_task('t1', {'kind': 'implement', 'done_when': PR_URL + ' が merge される'})
        self.assertEqual((changed['kind'], changed['done_when']), ('implement', PR_URL + ' が merge される'))
        for payload in ({'kind': ''}, {'kind': 'bogus'}, {'kind': 3}, {'kind': ['a']}, {'done_when': ''}, {'done_when': ' '}, {'done_when': 'あ' * 1001}, {'done_when': 3}, {'kind': 'monitor', 'done_when': ''}):
            self.assertEqual(self.status(self.store.update_task, 't1', payload), 400, payload)
        task = self.store.tasks()[0]
        self.assertEqual((task['kind'], task['done_when']), ('implement', PR_URL + ' が merge される'))

    def test_kind_と_done_when_の列が無い既存の_tasks_表へ列を足し旧行は空のまま読める(self):
        with contextlib.closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute('ALTER TABLE tasks DROP COLUMN kind')
            connection.execute('ALTER TABLE tasks DROP COLUMN done_when')
            connection.execute("INSERT INTO tasks(id, title, lane, created_at, updated_at) VALUES ('old', '旧行', 'worker', 'c', 'u')")
        store = Store(self.db)
        task = store.tasks()[0]
        self.assertEqual((task['id'], task['lane'], task['kind'], task['done_when']), ('old', 'worker', '', ''))
        self.assertEqual(store.update_task('old', {'remaining': '残り'})['remaining'], '残り')
        self.assertEqual(self.status(store.add_task, {'id': 'new', 'title': '新'}), 400)
        self.assertEqual(store.add_task({**TASK_DEFAULTS, 'id': 'new', 'title': '新'})['kind'], 'research')
        with contextlib.closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 2)

    def test_旧_release_の_INSERT_は_kind_と_done_when_を指定せず通り空になる(self):
        with contextlib.closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("INSERT INTO tasks(id, title, request, pane, run, result, remaining, decision_id, lane, created_at, updated_at) VALUES ('old', '旧', '', '', '', '', '', NULL, 'todo', 'c', 'u')")
        task = self.store.tasks()[0]
        self.assertEqual((task['kind'], task['done_when']), ('', ''))

    def test_回答しても_task_の_lane_と_remaining_は変わらない(self):
        decision = self.store.add(request_payload())
        other = self.store.add(request_payload(title='別の判断'))
        for identifier, lane, decision_id in (('t1', 'human_decision', decision['id']), ('t2', 'worker', decision['id']), ('t3', 'human_decision', other['id'])):
            seed_task(self.store, {'id': identifier, 'title': identifier, 'lane': lane, 'decision_id': decision_id, 'remaining': '残り'})
        keys = ('lane', 'remaining', 'updated_at', 'machine')
        before = {task['id']: [task[key] for key in keys] for task in self.store.tasks()}
        self.store.answer(decision['id'], {'choice': decision['options'][0]['id']})
        after = {task['id']: [task[key] for key in keys] for task in self.store.tasks()}
        self.assertEqual(after, before)

    def test_一覧は_lane_順で_lane_内は作成順(self):
        for identifier, lane in [('b', 'done'), ('a', 'worker'), ('c', 'todo'), ('d', 'worker')]:
            seed_task(self.store, {'id': identifier, 'title': identifier, 'lane': lane})
        self.store.update_task('a', {'title': 'z'})
        self.assertEqual([task['id'] for task in self.store.tasks()], ['c', 'a', 'd', 'b'])

    def test_削除と無い_id_の404(self):
        self.store.add_task({**TASK_DEFAULTS, 'id': 't1', 'title': '題'})
        self.assertEqual(self.store.delete_task('t1'), {'deleted': 't1'})
        self.assertEqual(self.store.tasks(), [])
        self.assertEqual(self.status(self.store.delete_task, 't1'), 404)

    def test_decision_id_の保存と検査(self):
        decision = self.store.add(request_payload())
        task = self.store.add_task({**TASK_DEFAULTS, 'id': 't1', 'title': '題', 'decision_id': decision['id']})
        self.assertEqual(task['decision_id'], decision['id'])
        self.assertEqual(self.status(self.store.update_task, 't1', {'decision_id': 'a' * 32}), 404)
        self.assertEqual(self.status(self.store.update_task, 't1', {'decision_id': 'zz'}), 400)
        self.assertIsNone(self.store.update_task('t1', {'decision_id': ''})['decision_id'])

    def test_一覧は判断の題名と回答の有無を添える(self):
        decision = self.store.add(request_payload(title='方針の判断'))
        self.store.add_task({**TASK_DEFAULTS, 'id': 't1', 'title': '題', 'decision_id': decision['id']})
        self.store.add_task({**TASK_DEFAULTS, 'id': 't2', 'title': '判断なし'})
        self.assertEqual([task['decision'] for task in self.store.tasks()], [{'title': '方針の判断', 'answered': False, 'withdrawn': False}, None])
        self.store.answer(decision['id'], {'choice': 'minimal'})
        self.assertEqual(self.store.tasks()[0]['decision'], {'title': '方針の判断', 'answered': True, 'withdrawn': False})

    def test_入力の検査(self):
        # kind と done_when は有効な値を足し、各入力が自分の理由で断られることを確かめる
        cases = [
            ({'id': 'a b', 'title': 'x'}, 'id'),
            ({'id': 'a', 'title': ' '}, 'title'),
            ({'id': 'a', 'title': 'x', 'extra': 1}, 'extra'),
            ({'id': 'a', 'title': 'x' * 201}, 'title'),
        ]
        for payload, reason in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(AppError) as caught:
                    self.store.add_task({**TASK_DEFAULTS, **payload})
                self.assertEqual(caught.exception.status, 400)
                self.assertIn(reason, str(caught.exception))

    def test_user_version_と判断は_task_を足しても変わらない(self):
        decision = self.store.add(request_payload())
        self.store.answer(decision['id'], {'choice': 'minimal'})
        self.store.add_task({**TASK_DEFAULTS, 'id': 't1', 'title': '題'})
        reopened = Store(self.db)
        self.assertEqual(reopened.get(decision['id'])['answer']['choice'], 'minimal')
        with contextlib.closing(sqlite3.connect(self.db)) as connection, connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 2)
            columns = [row[1] for row in connection.execute('PRAGMA table_info(decisions)')]
        self.assertEqual(columns, ['id', 'created_at', 'payload', 'choice', 'memo', 'decided_at', 'status', 'worker', 'token', 'lease_until', 'request_key', 'reason', 'next_step', 'reported_at'])

    def test_tasks_表が無い_version_2_の_DB_を開き直すと表ができる(self):
        decision = self.store.add(request_payload())
        with contextlib.closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute('DROP TABLE tasks')
        reopened = Store(self.db)
        self.assertEqual(reopened.tasks(), [])
        self.assertEqual(reopened.get(decision['id'])['id'], decision['id'])

    def test_backup_に_task_が入る(self):
        self.store.add_task({**TASK_DEFAULTS, 'id': 't1', 'title': '題'})
        output = Path(self.temp.name) / 'backup.sqlite3'
        self.store.backup(output)
        with contextlib.closing(sqlite3.connect(output)) as connection, connection:
            self.assertEqual(connection.execute('SELECT id FROM tasks').fetchall(), [('t1',)])


class TaskCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'cli.sqlite3'

    def cli(self, *arguments):
        return subprocess.run([sys.executable, str(ROOT / 'app.py'), '--db', str(self.db), 'task', *arguments], capture_output=True, text=True, timeout=10)

    def test_reconcile_lane_は_lane_を書き不正な_lane_と未登録の_id_を断る(self):
        self.cli('add', *ADD_KIND, '--id', 't1', '--title', '題')
        written = self.cli('reconcile-lane', '--id', 't1', '--lane', 'worker')
        self.assertEqual(written.returncode, 0, written.stderr)
        self.assertEqual(json.loads(written.stdout)['lane'], 'worker')
        self.assertEqual(Store(self.db).tasks()[0]['lane'], 'worker')
        for arguments in (('--id', 't1', '--lane', 'bogus'), ('--id', 'nope', '--lane', 'done'), ('--id', 't1')):
            self.assertNotEqual(self.cli('reconcile-lane', *arguments).returncode, 0, arguments)
        self.assertEqual(Store(self.db).tasks()[0]['lane'], 'worker')

    def test_reconcile_lane_の_from_は一致で書き競合は_exit_3_task_無しは_exit_1(self):
        self.cli('add', *ADD_KIND, '--id', 't1', '--title', '題')
        self.assertEqual(self.cli('reconcile-lane', '--id', 't1', '--lane', 'done').returncode, 0)
        conflict = self.cli('reconcile-lane', '--id', 't1', '--lane', 'todo', '--from', 'worker')
        self.assertEqual(conflict.returncode, 3, conflict.stderr)
        self.assertIn('lane が違う', json.loads(conflict.stderr)['error'])
        self.assertEqual(Store(self.db).tasks()[0]['lane'], 'done')
        missing = self.cli('reconcile-lane', '--id', 'nope', '--lane', 'todo', '--from', 'done')
        self.assertEqual(missing.returncode, 1)
        invalid = self.cli('reconcile-lane', '--id', 't1', '--lane', 'todo', '--from', 'bogus')
        self.assertEqual(invalid.returncode, 1)
        self.assertIn('--from', json.loads(invalid.stderr)['error'])
        matched = self.cli('reconcile-lane', '--id', 't1', '--lane', 'todo', '--from', 'done')
        self.assertEqual(matched.returncode, 0, matched.stderr)
        self.assertEqual(json.loads(matched.stdout)['lane'], 'todo')
        self.assertIn('--from', self.cli('reconcile-lane', '--help').stdout)

    def test_担当の条件付き更新は全条件を確認し他の項目を保つ(self):
        added = self.cli('add', *ADD_KIND, '--id', 't1', '--title', '題', '--run', 'run-a',
                         '--request', '依頼', '--remaining', '残り')
        self.assertEqual(added.returncode, 0, added.stderr)
        before = json.loads(added.stdout)
        expected = {'from': 'todo', 'from-pane': '', 'from-machine': '',
                    'from-run': before['run'], 'from-request': before['request'], 'from-kind': before['kind']}

        def assign(conditions, *, lane='worker', pane='w1:p2', machine='受理'):
            arguments = [argument for key, value in conditions.items() for argument in ('--' + key, value)]
            return self.cli('reconcile-lane', '--id', 't1', '--lane', lane, '--pane', pane,
                            '--machine', machine, *arguments)

        for field, other in (('from', 'done'), ('from-pane', 'w1:p9'), ('from-machine', '別の理由'),
                             ('from-run', 'run-b'), ('from-request', '別の依頼'), ('from-kind', 'monitor')):
            with self.subTest(field=field):
                conflict = assign({**expected, field: other})
                self.assertEqual(conflict.returncode, 3, conflict.stderr)
                self.assertTrue(json.loads(conflict.stderr)['error'])
                self.assertEqual(Store(self.db).tasks()[0], {**before, 'decision': None})

        for omitted in expected:
            with self.subTest(omitted=omitted):
                invalid = assign({key: value for key, value in expected.items() if key != omitted})
                self.assertEqual(invalid.returncode, 1, invalid.stderr)

        applied = assign(expected)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        after = json.loads(applied.stdout)
        self.assertEqual(after, {**before, 'lane': 'worker', 'pane': 'w1:p2', 'machine': '受理', 'updated_at': after['updated_at']})
        matched = assign({**expected, 'from': 'worker', 'from-pane': 'w1:p2', 'from-machine': '受理'})
        self.assertEqual(matched.returncode, 0, matched.stderr)
        self.assertEqual(json.loads(matched.stdout), after)
        cleared = assign({'from': 'worker', 'from-pane': 'w1:p2', 'from-machine': '受理'},
                         lane='todo', pane='', machine='')
        self.assertEqual(cleared.returncode, 0, cleared.stderr)
        self.assertEqual(json.loads(cleared.stdout)['pane'], '')

    def test_同じ担当を観測した二つの_CLI_のうち一方だけが更新する(self):
        added = self.cli('add', *ADD_KIND, '--id', 't1', '--title', '題')
        self.assertEqual(added.returncode, 0, added.stderr)

        def assign(pane):
            return self.cli('reconcile-lane', '--id', 't1', '--lane', 'worker', '--pane', pane,
                            '--machine', pane, '--from', 'todo', '--from-pane', '', '--from-machine', '',
                            '--from-run', '', '--from-request', '', '--from-kind', 'research')

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(assign, ('w1:p1', 'w2:p1')))
        self.assertEqual(sorted(result.returncode for result in results), [0, 3], results)
        winner = json.loads(next(result.stdout for result in results if result.returncode == 0))
        self.assertEqual(winner['pane'], winner['machine'])
        self.assertEqual(Store(self.db).tasks()[0], {**winner, 'decision': None})

    def test_完了時に観測した残件と判断_ID_と更新時刻を照合する(self):
        store = Store(self.db)
        decision = store.add(request_payload())
        added = self.cli('add', *ADD_KIND, '--id', 't1', '--title', '題', '--remaining', 'なし')
        original = json.loads(added.stdout)
        edited = store.update_task('t1', {'result': '新しい調査結果'})

        def settle(remaining='なし', decision_id='', updated_at=edited['updated_at']):
            return self.cli('reconcile-lane', '--id', 't1', '--lane', 'done', '--from', 'todo',
                            '--from-remaining', remaining, '--from-decision-id', decision_id,
                            '--from-updated-at', updated_at)

        for condition in ({'remaining': '残りあり'}, {'decision_id': decision['id']},
                          {'updated_at': original['updated_at']}):
            with self.subTest(condition=condition):
                conflict = settle(**condition)
                self.assertEqual(conflict.returncode, 3, conflict.stderr)
                self.assertEqual(store.tasks()[0], {**edited, 'decision': None})

        partial = self.cli('reconcile-lane', '--id', 't1', '--lane', 'done', '--from-remaining', 'なし')
        self.assertEqual(partial.returncode, 1, partial.stderr)
        self.assertEqual(store.tasks()[0]['lane'], 'todo')
        applied = settle()
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual(json.loads(applied.stdout)['lane'], 'done')

    def test_時計が進まなくても_task_の変更前の時刻では完了できない(self):
        store = Store(self.db)
        with patch('app.utc_now', return_value='2026-11-29T00:00:00.000000+00:00'):
            before = store.add_task({**TASK_DEFAULTS, 'id': 't1', 'title': '題', 'remaining': 'なし'})
            edited = store.update_task('t1', {'result': '新しい調査結果'})
            moved = store.reconcile_lane('t1', 'worker')
        self.assertNotEqual(edited['updated_at'], before['updated_at'])
        self.assertNotEqual(moved['updated_at'], edited['updated_at'])
        stale = self.cli('reconcile-lane', '--id', 't1', '--lane', 'done', '--from', 'worker',
                         '--from-remaining', 'なし', '--from-decision-id', '',
                         '--from-updated-at', before['updated_at'])
        self.assertEqual(stale.returncode, 3, stale.stderr)

    def test_紐付いた判断の現在の状態を見て完了を許可する(self):
        store = Store(self.db)
        other = store.add(request_payload())
        store.answer(other['id'], {'choice': 'minimal'})
        store.complete(other['id'], store.claim('worker')['token'])
        decision = store.add(request_payload())
        answered = store.answer(decision['id'], {'choice': 'minimal'})
        added = self.cli('add', *ADD_KIND, '--id', 't1', '--title', '題', '--remaining', 'なし',
                         '--decision', decision['id'])
        observed = json.loads(added.stdout)

        def settle(task=observed):
            return self.cli('reconcile-lane', '--id', task['id'], '--lane', 'done', '--from', 'todo',
                            '--from-remaining', 'なし', '--from-decision-id', task['decision_id'],
                            '--from-updated-at', task['updated_at'])

        pending = settle()
        self.assertEqual(pending.returncode, 3, pending.stderr)
        store.undo(decision['id'], {'decided_at': answered['answer']['decided_at']})
        undone = settle()
        self.assertEqual(undone.returncode, 3, undone.stderr)
        self.assertEqual(store.tasks()[0]['lane'], 'todo')
        store.withdraw(decision['id'], {'note': '判断済みの別依頼に集約'})
        applied = settle()
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual(json.loads(applied.stdout)['lane'], 'done')

        completed = store.add(request_payload())
        store.answer(completed['id'], {'choice': 'minimal'})
        task = store.add_task({**TASK_DEFAULTS, 'id': 't2', 'title': '題', 'remaining': 'なし',
                               'decision_id': completed['id']})
        claim = store.claim('worker')
        self.assertEqual(settle(task).returncode, 3)
        store.complete(completed['id'], claim['token'])
        closed = settle(task)
        self.assertEqual(closed.returncode, 0, closed.stderr)
        self.assertEqual(json.loads(closed.stdout)['lane'], 'done')

    def test_move_と_add_の_lane_を断り_lane_は変わらない(self):
        self.cli('add', *ADD_KIND, '--id', 't1', '--title', '題')
        for arguments in (('move', '--id', 't1', '--lane', 'done'), ('add', '--id', 't2', '--title', 'a', '--lane', 'worker')):
            refused = self.cli(*arguments)
            self.assertNotEqual(refused.returncode, 0, arguments)
            output = refused.stdout + refused.stderr
            self.assertIn('lane は reconciler が書く', output)
            self.assertNotIn('reconcile-lane', output)
        tasks = Store(self.db).tasks()
        self.assertEqual([(t['id'], t['lane']) for t in tasks], [('t1', 'todo')])
        self.assertIn('reconcile-lane', self.cli('--help').stdout)
        for name in ('add', 'move'):
            self.assertNotIn('--lane', self.cli(name, '--help').stdout)

    def test_reconcile_lane_は機械の欄を書き_update_の_remaining_と共存する(self):
        self.cli('add', *ADD_KIND, '--id', 't1', '--title', '題')
        self.cli('update', '--id', 't1', '--remaining', '残り')
        written = self.cli('reconcile-lane', '--id', 't1', '--lane', 'todo', '--machine', 'Cue 回答済み')
        self.assertEqual(written.returncode, 0, written.stderr)
        task = Store(self.db).tasks()[0]
        self.assertEqual((task['remaining'], task['machine']), ('残り', 'Cue 回答済み'))
        self.assertNotEqual(self.cli('update', '--id', 't1', '--machine', 'x').returncode, 0)
        self.assertIn('機械: Cue 回答済み', self.cli('list').stdout)
        # --lane を付けない move も argparse でなく Store の断りに届く
        self.assertIn('lane は reconciler が書く', self.cli('move', '--id', 't1').stderr)

    def test_kind_と_done_when_が欠けた_add_は_JSON_の_error_で断る(self):
        for arguments in (('--id', 't1', '--title', '題'), ('--id', 't1', '--title', '題', '--kind', 'research'), ('--id', 't1', '--title', '題', '--done-when', '終わる'),
                          ('--id', 't1', '--title', '題', '--kind', 'bogus', '--done-when', '終わる'), ('--id', 't1', '--title', '題', '--kind', 'research', '--done-when', ' ')):
            refused = self.cli('add', *arguments)
            self.assertEqual((refused.returncode, refused.stdout), (1, ''), arguments)
            self.assertIn('error', json.loads(refused.stderr))
        self.assertEqual(Store(self.db).tasks(), [])
        self.assertIn('kind', self.cli('add', '--help').stdout)
        self.assertIn('implement', self.cli('add', '--help').stdout)

    def test_kind_と_done_when_を_add_と_update_で扱い_list_に出す(self):
        added = self.cli('add', '--id', 't1', '--title', '題', '--kind', 'monitor', '--done-when', 'CI が緑になる')
        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertEqual((json.loads(added.stdout)['kind'], json.loads(added.stdout)['done_when']), ('monitor', 'CI が緑になる'))
        self.assertEqual(self.cli('update', '--id', 't1', '--kind', 'implement').returncode, 1)
        updated = json.loads(self.cli('update', '--id', 't1', '--kind', 'implement', '--done-when', PR_URL + ' が merge される').stdout)
        self.assertEqual((updated['kind'], updated['done_when']), ('implement', PR_URL + ' が merge される'))
        refused = self.cli('update', '--id', 't1', '--kind', 'bogus')
        self.assertEqual(refused.returncode, 1)
        self.assertIn('error', json.loads(refused.stderr))
        self.assertEqual(self.cli('update', '--id', 't1', '--done-when', '').returncode, 1)
        listed = self.cli('list').stdout
        self.assertIn('種類: implement', listed)
        self.assertIn('完了の条件: ' + PR_URL + ' が merge される', listed)
        self.assertEqual(json.loads(self.cli('list', '--json').stdout)['lanes'][0]['tasks'][0]['kind'], 'implement')

    def test_task_サブコマンドを通す(self):
        added = self.cli('add', *ADD_KIND, '--id', 't1', '--title', '題', '--pane', 'p1')
        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertEqual(json.loads(added.stdout)['lane'], 'todo')
        self.assertEqual(json.loads(self.cli('reconcile-lane', '--id', 't1', '--lane', 'human_decision').stdout)['lane'], 'human_decision')
        updated = json.loads(self.cli('update', '--id', 't1', '--result', 'ok', '--pane', '').stdout)
        self.assertEqual((updated['result'], updated['pane'], updated['title']), ('ok', '', '題'))
        listed = self.cli('list')
        self.assertEqual(listed.stdout.count('## '), 5)
        self.assertIn('## 人の判断待ち (1)', listed.stdout)
        self.assertIn('- 題  [t1]', listed.stdout)
        data = json.loads(self.cli('list', '--json').stdout)
        self.assertEqual([lane['id'] for lane in data['lanes']], ['todo', 'human_decision', 'worker', 'human_action', 'done'])
        only = json.loads(self.cli('list', '--json', '--lane', 'done').stdout)
        self.assertEqual(len(only['lanes']), 1)
        invalid = self.cli('list', '--json', '--lane', 'bogus')
        self.assertEqual((invalid.returncode, invalid.stdout), (1, ''))
        decision = Store(self.db).add(request_payload(title='方針の判断'))
        self.cli('update', '--id', 't1', '--decision', decision['id'])
        self.assertIn(f"判断: 未回答 方針の判断  [{decision['id']}]", self.cli('list').stdout)
        data = json.loads(self.cli('list', '--json').stdout)
        self.assertEqual(data['lanes'][1]['tasks'][0]['decision'], {'title': '方針の判断', 'answered': False, 'withdrawn': False})
        self.assertEqual(json.loads(self.cli('delete', '--id', 't1').stdout), {'deleted': 't1'})
        failed = self.cli('delete', '--id', 't1')
        self.assertEqual(failed.returncode, 1)
        self.assertIn('error', json.loads(failed.stderr))


SOCKET_A = Path.home() / '.config' / 'herdr' / 'herdr.sock'
SOCKET_B = Path.home() / '.config' / 'herdr' / 'sessions' / 'workers' / 'herdr.sock'


class FocusPaneTests(unittest.TestCase):
    def setUp(self):
        self.calls, self.runs = [], []

    def herdr(self, owners, broken=()):
        # owners: socket -> pane を持つか。broken の socket は接続できない
        def call(path, method, params):
            self.calls.append((path, method, params['pane_id']))
            if path in broken:
                raise ConnectionRefusedError()
            if method == 'pane.focus' and self.focus_error:
                return {'error': {'code': 'x', 'message': '失敗した'}}
            if owners.get(path):
                return {'result': {}}
            return {'error': {'code': 'pane_not_found', 'message': 'なし'}}
        return call

    focus_error = False

    def go(self, owners, clients=lambda: {'default': '%1', 'workers': '%7'}, own=SOCKET_A, broken=(), pane='w9:p1', sessions=None):
        sessions = sessions or (lambda: [('default', SOCKET_A), ('workers', SOCKET_B)])
        return focus_pane(pane, sessions, self.herdr(owners, broken), clients, self.runs.append, str(own))

    def test_pane_が空なら移らない(self):
        self.assertEqual(self.go({}, pane='  '), (False, 'この task には担当 pane がありません'))
        self.assertEqual(self.calls, [])

    def test_どこにも無い(self):
        self.assertEqual(self.go({}), (False, '担当 pane w9:p1 は herdr にもうありません'))

    def test_2_session_にあると移らない(self):
        ok, message = self.go({SOCKET_A: True, SOCKET_B: True})
        self.assertFalse(ok)
        self.assertIn('複数の session (default, workers)', message)
        self.assertNotIn('pane.focus', [call[1] for call in self.calls])

    def test_全_socket_に接続できない(self):
        self.assertEqual(self.go({}, broken=(SOCKET_A, SOCKET_B)), (False, 'herdr に接続できないため移れません'))
        self.assertEqual(self.go({}, sessions=lambda: []), (False, 'herdr に接続できないため移れません'))

    def test_接続できない_socket_は飛ばす(self):
        self.assertEqual(self.go({SOCKET_A: True}, broken=(SOCKET_B,)), (True, 'w9:p1 (default) へ移りました'))

    def test_同じ_session_なら_tmux_を触らない(self):
        def no_clients():
            raise AssertionError('tmux を呼んではいけない')
        self.assertEqual(self.go({SOCKET_A: True}, clients=no_clients), (True, 'w9:p1 (default) へ移りました'))
        self.assertEqual(self.runs, [])

    def test_別_session_は_focus_と_tmux_の_window_切り替えをする(self):
        self.assertEqual(self.go({SOCKET_B: True}), (True, 'w9:p1 (workers) へ移りました'))
        self.assertIn((SOCKET_B, 'pane.focus', 'w9:p1'), self.calls)
        self.assertEqual(self.runs, [['tmux', 'select-window', '-t', '%7'], ['tmux', 'select-pane', '-t', '%7']])

    def test_別_session_の_client_が無ければ_focus_しない(self):
        result = self.go({SOCKET_B: True}, clients=lambda: {'default': '%1'})
        self.assertEqual(result, (False, 'workers session を開いている端末が無いため移れません'))
        self.assertNotIn('pane.focus', [call[1] for call in self.calls])

    def test_tmux_が使えなければ_focus_だけ行う(self):
        ok, message = self.go({SOCKET_B: True}, clients=lambda: None)
        self.assertTrue(ok)
        self.assertEqual(message, 'w9:p1 (workers) を focus しました。tmux が無いため端末は切り替えていません')
        self.assertIn((SOCKET_B, 'pane.focus', 'w9:p1'), self.calls)
        self.assertEqual(self.runs, [])

    def test_pane_focus_の_error(self):
        self.focus_error = True
        self.assertEqual(self.go({SOCKET_A: True}), (False, 'w9:p1 へ移れませんでした: 失敗した'))
        self.assertEqual(self.runs, [])

    def test_tmux_の_select_が失敗したら報告する(self):
        def failing(argv):
            raise subprocess.CalledProcessError(1, argv)
        result = focus_pane('w9:p1', lambda: [('workers', SOCKET_B)], self.herdr({SOCKET_B: True}), lambda: {'workers': '%7'}, failing, str(SOCKET_A))
        self.assertEqual(result, (False, 'herdr の focus は移しましたが、端末の window を切り替えられませんでした'))


class TmuxClientTests(unittest.TestCase):
    PANES = '%1\t100\n%2\t200\n%3\t300\n%4\t400\n'

    def clients(self, table):
        def run(argv):
            return self.PANES if argv[:2] == ['tmux', 'list-panes'] else table
        return tmux_clients(run)

    def test_引数から_session_を読む(self):
        table = '100 1 -zsh\n101 100 herdr\n200 1 herdr --session workers\n300 1 /usr/bin/herdr session attach review\n'
        self.assertEqual(self.clients(table), {'default': '%1', 'workers': '%2', 'review': '%3'})

    def test_pane_pid_自身が_client_でも拾う(self):
        self.assertEqual(self.clients('400 1 herdr --session=x\n'), {'x': '%4'})

    def test_孫以下の_herdr_は_client_として拾わない(self):
        table = '100 1 -zsh\n101 100 sh -c run\n102 101 herdr pane list\n103 101 herdr --session x\n200 1 herdr pane list\n'
        self.assertEqual(self.clients(table), {})

    def test_tmux_が使えなければ_None(self):
        def run(argv):
            raise FileNotFoundError('tmux')
        self.assertIsNone(tmux_clients(run))


class TaskTuiTests(unittest.TestCase):
    def make(self, directory, popup=False, results=((True, '移りました'),)):
        store = Store(Path(directory) / 'tui.sqlite3')
        seed_task(store, {'id': 't1', 'title': '一つ目', 'lane': 'worker', 'pane': 'w9:p1'})
        seed_task(store, {'id': 't2', 'title': '二つ目', 'lane': 'todo', 'pane': 'w9:p2'})
        seed_task(store, {'id': 't3', 'title': '三つ目', 'lane': 'worker', 'pane': 'w9:p3'})
        self.focused = []

        def focus(pane):
            self.focused.append(pane)
            return results[0]
        desk = Desk(store, focus=focus, popup=popup)
        handle_key(desk, 'b')
        return store, desk

    def test_上下で選択が表示順に動き端で止まる(self):
        with tempfile.TemporaryDirectory() as directory:
            _, desk = self.make(directory)
            # 表示順は todo の t2、worker の t1 t3
            self.assertEqual(desk.task_id, 't2')
            for key, expected in [('j', 't1'), ('j', 't3'), ('j', 't3'), ('k', 't1'), ('k', 't2'), ('k', 't2')]:
                handle_key(desk, key)
                self.assertEqual(desk.task_id, expected)

    def test_完了は既定で畳まれ件数が見え_d_で開閉し畳むと選択が見える行へ戻る(self):
        with tempfile.TemporaryDirectory() as directory:
            store, desk = self.make(directory)
            for identifier in ('d1', 'd2'):
                seed_task(store, {'id': identifier, 'title': '終わった ' + identifier, 'lane': 'done', 'pane': 'w9:p9'})
            desk.refresh()

            def screen_text():
                screen = Screen(24, 100)
                with patch('cue_tui.curses.doupdate'):
                    draw(screen, desk)
                return '\n'.join(screen.output)
            text = screen_text()
            self.assertIn('完了 (2) d で開く', text)
            self.assertNotIn('終わった d1', text)
            for _ in range(5):
                handle_key(desk, 'j')
            self.assertEqual(desk.task_id, 't3')
            handle_key(desk, 'd')
            self.assertIn('完了 (2) d で閉じる', screen_text())
            self.assertIn('終わった d1', screen_text())
            handle_key(desk, 'j')
            self.assertEqual(desk.task_id, 'd1')
            handle_key(desk, 'd')
            self.assertNotIn('終わった d1', screen_text())
            self.assertIn(desk.task_id, ('t1', 't2', 't3'))
            handle_key(desk, 'd')
            handle_key(desk, 'b')
            handle_key(desk, 'b')
            self.assertFalse(desk.done_open)

    def test_未知の_lane_の行があっても上下で_IndexError_にならない(self):
        with tempfile.TemporaryDirectory() as directory:
            _, desk = self.make(directory)
            desk.tasks.append({'id': 'x', 'title': '不明', 'lane': 'unknown', 'pane': 'w9:p9'})
            for key in ['j', 'j', 'j', 'j', 'k', 'k', 'k', 'k', 'k']:
                handle_key(desk, key)
            self.assertEqual(desk.task_id, 't2')

    def test_選択を動かすと前の_message_が消える(self):
        with tempfile.TemporaryDirectory() as directory:
            _, desk = self.make(directory)
            desk.task_message = '前の結果'
            handle_key(desk, 'j')
            self.assertEqual(desk.task_message, '')

    def test_popup_でも_tmux_注記付きの成功なら閉じずに_message_を残す(self):
        note = '。tmux が無いため端末は切り替えていません'
        with tempfile.TemporaryDirectory() as directory:
            _, desk = self.make(directory, popup=True, results=((True, 'w9:p2 (workers) を focus しました' + note),))
            self.assertTrue(handle_key(desk, '\n'))
            self.assertTrue(desk.task_message.endswith(note))

    def test_Enter_で選択中_task_の_pane_へ移り_message_が出て_tasks_のまま(self):
        with tempfile.TemporaryDirectory() as directory:
            _, desk = self.make(directory)
            handle_key(desk, 'j')
            self.assertTrue(handle_key(desk, '\n'))
            self.assertEqual(self.focused, ['w9:p1'])
            self.assertEqual((desk.mode, desk.task_id), ('tasks', 't1'))
            screen = Screen(24, 100)
            with patch('cue_tui.curses.doupdate'):
                draw(screen, desk)
            self.assertIn('移りました', '\n'.join(screen.output))

    def test_失敗しても残り選択は変わらない(self):
        with tempfile.TemporaryDirectory() as directory:
            _, desk = self.make(directory, popup=True, results=((False, '移れません'),))
            self.assertTrue(handle_key(desk, '\n'))
            self.assertEqual((desk.mode, desk.task_id, desk.task_message), ('tasks', 't2', '移れません'))

    def test_popup_で成功すると終了する(self):
        with tempfile.TemporaryDirectory() as directory:
            _, desk = self.make(directory, popup=True)
            self.assertFalse(handle_key(desk, '\n'))
            self.assertEqual(self.focused, ['w9:p2'])

    def test_popup_で未保存入力があると_focus_を呼ばない(self):
        with tempfile.TemporaryDirectory() as directory:
            store, desk = self.make(directory, popup=True)
            decision = store.add(request_payload())
            desk.refresh()
            desk.drafts[decision['id']] = {'choice': None, 'memo': '書きかけ'}
            self.assertTrue(handle_key(desk, '\n'))
            self.assertEqual(self.focused, [])
            self.assertIn('未保存の入力があるため移れません', desk.task_message)

    def test_再読み込みで選択は同じ_task_を保ち消えたら丸める(self):
        with tempfile.TemporaryDirectory() as directory:
            store, desk = self.make(directory)
            handle_key(desk, 'j')
            handle_key(desk, 'j')
            self.assertEqual(desk.task_id, 't3')
            seed_task(store, {'id': 't0', 'title': '先頭', 'lane': 'todo', 'pane': 'w9:p0'})
            desk.refresh()
            self.assertEqual((desk.task_id, desk.task_index), ('t3', 3))
            store.delete_task('t3')
            desk.refresh()
            self.assertEqual((desk.task_id, desk.task_index), ('t1', 2))

    def test_選択中の題名行を反転しスクロールが追従する(self):
        with tempfile.TemporaryDirectory() as directory:
            store, desk = self.make(directory)
            for number in range(20):
                seed_task(store, {'id': f'x{number}', 'title': f'件{number}', 'lane': 'done'})
            desk.refresh()
            handle_key(desk, 'd')
            for _ in range(22):
                handle_key(desk, 'j')
            attributes = {}
            screen = Screen(12, 100)
            screen.addstr = lambda row, column, value, attribute: attributes.__setitem__(value, attribute)
            with patch('cue_tui.curses.doupdate'):
                draw(screen, desk)
            self.assertGreater(desk.task_scroll, 0)
            self.assertEqual(attributes['> 件19'], curses.A_REVERSE | curses.A_BOLD)

    def test_判断つきの_task_を選ぶと判断の行まで見える位置へ追従する(self):
        with tempfile.TemporaryDirectory() as directory:
            store, desk = self.make(directory)
            decision = store.add(request_payload(title='途中の判断'))
            seed_task(store, {'id': 'd1', 'title': '判断つき', 'lane': 'human_decision', 'decision_id': decision['id']})
            # 判断の行が画面の下端を越える位置に来るよう、前に task を積む
            for number in range(6):
                seed_task(store, {'id': f'y{number}', 'title': f'前{number}', 'lane': 'todo'})
            desk.refresh()
            while desk.task_id != 'd1':
                handle_key(desk, 'j')
            screen = Screen(12, 100)
            with patch('cue_tui.curses.doupdate'):
                draw(screen, desk)
            output = '\n'.join(screen.output)
            self.assertIn('> 判断つき', output)
            self.assertIn('    判断 未回答 途中の判断', output)

    def test_人待ちの_lane_と判断の状態を色と記号で分ける(self):
        with tempfile.TemporaryDirectory() as directory:
            store, desk = self.make(directory)
            decision = store.add(request_payload(title='未回答の判断'))
            seed_task(store, {'id': 'w1', 'title': '判断待ちの依頼', 'lane': 'human_decision', 'decision_id': decision['id']})
            answered = store.add(request_payload(title='回答済みの判断', request_key='answered'))
            store.answer(answered['id'], {'choice': 'minimal'})
            seed_task(store, {'id': 'w2', 'title': '作業中の依頼', 'lane': 'worker', 'decision_id': answered['id']})
            desk.refresh()
            for styles in ({}, {'stopped': curses.A_BOLD | 0x100, 'recommendation': curses.A_BOLD | 0x200}):
                desk.heading_styles = styles
                attributes = {}
                screen = Screen(40, 100)
                screen.addstr = lambda row, column, value, attribute: attributes.__setitem__(value, attribute)
                with patch('cue_tui.curses.doupdate'):
                    draw(screen, desk)
                waiting = styles.get('stopped', curses.A_BOLD)
                self.assertEqual(attributes['▲ 人の判断待ち (1)'], waiting)
                self.assertEqual(attributes['▲ 人の操作待ち (0)'], curses.A_BOLD)
                self.assertEqual(attributes['▸ 完了 (0)'], curses.A_BOLD)
                self.assertEqual(next(value for key, value in attributes.items() if key.startswith('    判断 未回答 未回答の判断')), waiting)
                answered = next(value for key, value in attributes.items() if key.startswith('    判断 回答済み'))
                self.assertEqual(answered, styles.get('recommendation', 0) & ~curses.A_BOLD)


    def test_b_で_task_一覧を開閉できる(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'tui.sqlite3')
            decision = store.add(request_payload(title='方針の判断'))
            store.answer(decision['id'], {'choice': 'minimal'})
            seed_task(store, {'id': 't1', 'title': '実装中の依頼', 'lane': 'worker', 'pane': 'p1', 'decision_id': decision['id']})
            desk = Desk(store)
            self.assertTrue(handle_key(desk, 'b'))
            self.assertEqual(desk.mode, 'tasks')
            for key in ('j', 'k'):
                self.assertTrue(handle_key(desk, key))
            screen = Screen(24, 100)
            with patch('cue_tui.curses.doupdate'):
                draw(screen, desk)
            output = '\n'.join(screen.output)
            self.assertIn('worker 作業中 (1)', output)
            self.assertIn('実装中の依頼', output)
            self.assertIn('判断 回答済み 方針の判断', output)
            self.assertIn('完了 (0)', output)
            handle_key(desk, 'b')
            self.assertIsNone(desk.mode)

    def test_task_の読み込み失敗は判断の表示と分けて_task_画面に出す(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'tui.sqlite3')
            store.add(request_payload())
            with patch.object(Store, 'tasks', side_effect=sqlite3.OperationalError('locked')):
                desk = Desk(store)
                self.assertEqual(desk.load_error, '')
                handle_key(desk, 'b')
            screen = Screen(24, 100)
            with patch('cue_tui.curses.doupdate'):
                draw(screen, desk)
            self.assertIn('task を読み込めません', '\n'.join(screen.output))


if __name__ == '__main__':
    unittest.main()
