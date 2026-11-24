import http.client
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import AppError, Store, make_server
from test_app import request_payload, seed_task


class BoardStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'board.sqlite3'
        self.store = Store(self.db)

    def status(self, call, *args, **kwargs):
        with self.assertRaises(AppError) as caught:
            call(*args, **kwargs)
        return caught.exception.status

    def open_worker(self, run='run-1', pane='w1:p2', session='uuid-worker-1'):
        self.store.open_session({'run_id': run, 'role': 'worker', 'pane': pane, 'socket': '/tmp/herdr.sock',
                                 'cli': 'claude', 'native_session_id': session})

    def test_投稿は人待ちの_lane_から想定内を導き現在の担当を写す(self):
        seed_task(self.store, {'id': 't1', 'title': '判断の依頼', 'kind': 'research', 'lane': 'human_decision',
                               'run': 'run-1', 'pane': 'w1:p2'})
        self.open_worker()
        post = self.store.create_post('t1', {'body': '確認しました'})
        self.assertEqual(post['classification'], 'expected')
        self.assertEqual(post['state'], 'unreceived')
        self.assertEqual(post['target_run'], 'run-1')
        self.assertEqual(post['target_pane'], 'w1:p2')
        self.assertEqual(post['target_session'], 'uuid-worker-1')
        self.assertIsNone(post['received_at'])

    def test_投稿の宛先は担当_pane_の_worker_で後に開いた別_worker_ではない(self):
        # issue #54 の再現: 同じ run で担当外の worker が後に開いても、宛先は担当 pane の worker
        seed_task(self.store, {'id': 't1', 'title': '判断の依頼', 'kind': 'research', 'lane': 'human_decision',
                               'run': 'run-1', 'pane': 'w1:p2'})
        self.open_worker(pane='w1:p2', session='uuid-worker-1')
        self.open_worker(pane='w1:p3', session='uuid-worker-2')
        post = self.store.create_post('t1', {'body': '確認しました'})
        self.assertEqual(post['target_pane'], 'w1:p2')
        self.assertEqual(post['target_session'], 'uuid-worker-1')

    def test_投稿は担当_pane_が空なら同じ_run_に_worker_がいても宛先なし(self):
        seed_task(self.store, {'id': 't1', 'title': '実装の依頼', 'kind': 'research', 'lane': 'worker', 'run': 'run-1'})
        self.open_worker(pane='w1:p2', session='uuid-worker-1')
        post = self.store.create_post('t1', {'body': '進み具合を教えてください'})
        self.assertEqual(post['state'], 'unreceived')
        self.assertEqual(post['target_run'], '')
        self.assertEqual(post['target_pane'], '')
        self.assertEqual(post['target_session'], '')

    def test_投稿は担当_pane_に開いた_session_がなければ宛先なし(self):
        seed_task(self.store, {'id': 't1', 'title': '実装の依頼', 'kind': 'research', 'lane': 'worker',
                               'run': 'run-1', 'pane': 'w1:p9'})
        self.open_worker(pane='w1:p2', session='uuid-worker-1')
        post = self.store.create_post('t1', {'body': '連絡'})
        self.assertEqual(post['target_session'], '')
        # 担当 pane の session が閉じている場合も宛先なし
        seed_task(self.store, {'id': 't2', 'title': '別の依頼', 'kind': 'research', 'lane': 'worker',
                               'run': 'run-1', 'pane': 'w1:p2'})
        self.store.close_session(cli='claude', native_session_id='uuid-worker-1', reason='normal')
        post = self.store.create_post('t2', {'body': '連絡'})
        self.assertEqual(post['target_session'], '')

    def test_投稿は担当_pane_の_session_が一意でなければ宛先なし(self):
        seed_task(self.store, {'id': 't1', 'title': '実装の依頼', 'kind': 'research', 'lane': 'worker',
                               'run': 'run-1', 'pane': 'w1:p2'})
        self.open_worker(pane='w1:p2', session='uuid-worker-1')
        self.store.open_session({'run_id': 'run-1', 'role': 'cue_worker', 'pane': 'w1:p2', 'socket': '/tmp/other.sock',
                                 'cli': 'claude', 'native_session_id': 'uuid-cue-1'})
        post = self.store.create_post('t1', {'body': '連絡'})
        self.assertEqual(post['target_session'], '')

    def test_投稿は作業中の_lane_では想定外で担当不在なら宛先なしで残る(self):
        seed_task(self.store, {'id': 't1', 'title': '実装の依頼', 'kind': 'research', 'lane': 'worker', 'run': 'run-1'})
        post = self.store.create_post('t1', {'body': '進み具合を教えてください'})
        self.assertEqual(post['classification'], 'unexpected')
        self.assertEqual(post['target_run'], '')
        self.assertEqual(post['target_session'], '')
        # 担当が後から付いても、投稿は未受領のまま受領対象として残る
        self.open_worker()
        received = self.store.receive_post(post['id'], {'received_by': 'uuid-worker-1'})
        self.assertEqual(received['state'], 'received')

    def test_受領は一度だけで分類と宛先と投稿時刻を変えない(self):
        seed_task(self.store, {'id': 't1', 'title': '判断の依頼', 'kind': 'research', 'lane': 'human_action', 'run': 'run-1'})
        self.open_worker()
        post = self.store.create_post('t1', {'body': '操作しました'})
        received = self.store.receive_post(post['id'], {'received_by': 'uuid-worker-1'})
        self.assertEqual(received['state'], 'received')
        self.assertIsNotNone(received['received_at'])
        self.assertEqual(received['received_by'], 'uuid-worker-1')
        for key in ('classification', 'target_run', 'target_pane', 'target_session', 'created_at', 'body'):
            self.assertEqual(received[key], post[key])
        self.assertEqual(self.status(self.store.receive_post, post['id'], {}), 409)
        self.assertEqual(self.status(self.store.receive_post, 9999, {}), 404)

    def test_投稿の入力検査(self):
        seed_task(self.store, {'id': 't1', 'title': '依頼', 'kind': 'research'})
        self.assertEqual(self.status(self.store.create_post, 'missing', {'body': 'x'}), 404)
        self.assertEqual(self.status(self.store.create_post, 't1', {'body': ' '}), 400)
        self.assertEqual(self.status(self.store.create_post, 't1', {'body': 'x', 'classification': 'expected'}), 400)
        self.assertEqual(self.status(self.store.create_post, 't1', {}), 400)
        self.assertEqual(self.store.posts()['posts'], [])

    def test_thread_は_msg_のメタデータと投稿と判断要約を混ぜて返す(self):
        decision = self.store.add(request_payload(title='方針の判断'))
        seed_task(self.store, {'id': 't1', 'title': '判断の依頼', 'kind': 'decision', 'done_when': '判断の回答が記録される',
                               'decision_id': decision['id'], 'lane': 'human_decision', 'run': 'run-1', 'pane': 'w1:p2'})
        self.open_worker()
        self.store.import_message({'run_id': 'run-1', 'msg': '1-1730000000000000-report-w1:p1-ab12.msg',
                                   'state': 'held', 'wait': f"decision={decision['id']}", 'req': 't1'})
        self.store.create_post('t1', {'body': '回答をお願いします'})
        thread = self.store.thread('t1')
        self.assertEqual(thread['task']['decision'], {'title': '方針の判断', 'answered': False, 'withdrawn': False})
        self.assertEqual(len(thread['messages']), 1)
        # messages 表は本文を持たないので、thread でも本文は返らない
        self.assertNotIn('body', thread['messages'][0])
        self.assertEqual(thread['messages'][0]['state'], 'held')
        self.assertEqual(len(thread['sessions']), 1)
        self.assertEqual(thread['posts'][0]['body'], '回答をお願いします')
        self.assertEqual(self.status(self.store.thread, 'missing'), 404)

    def test_unread_は未受領の配送試行と未回答の判断待ちの件数(self):
        self.store.add(request_payload(title='未回答の判断'))
        answered = self.store.add(request_payload(title='回答済みの判断'))
        self.store.answer(answered['id'], {'choice': 'minimal', 'memo': ''})
        seed_task(self.store, {'id': 't1', 'title': '依頼', 'kind': 'research', 'lane': 'worker'})
        post = self.store.create_post('t1', {'body': '連絡'})
        self.assertEqual(self.store.unread(), {'posts': 1, 'decisions': 1})
        self.store.receive_post(post['id'], {})
        self.assertEqual(self.store.unread(), {'posts': 0, 'decisions': 1})

    def test_posts_表を足しても_user_version_は_2_のまま(self):
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 2)
            self.assertIsNotNone(connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='posts'").fetchone())


class BoardHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'board-http.sqlite3'
        self.server = None
        self.start()
        self.addCleanup(self.stop)

    def start(self, demo=False):
        self.server = make_server(self.db, port=0, demo=demo)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=3)
            self.server = None

    def request(self, method, path, payload=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            body = json.dumps(payload).encode() if payload is not None else None
            headers = {'Content-Type': 'application/json'} if body is not None else {}
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            content = response.read()
            data = json.loads(content) if 'application/json' in response.getheader('Content-Type', '') else content
            return response.status, data
        finally:
            connection.close()

    def test_http_board_と_thread_と投稿と受領(self):
        store = Store(self.db)
        seed_task(store, {'id': 't1', 'title': '判断の依頼', 'kind': 'research', 'lane': 'human_decision',
                          'run': 'run-1', 'pane': 'w1:p2'})
        store.open_session({'run_id': 'run-1', 'role': 'worker', 'pane': 'w1:p2', 'socket': '/tmp/herdr.sock',
                            'cli': 'claude', 'native_session_id': 'uuid-worker-1'})
        status, board = self.request('GET', '/api/board')
        self.assertEqual(status, 200)
        self.assertEqual(len(board['sessions']), 1)
        self.assertEqual(board['unread'], {'posts': 0, 'decisions': 0})
        status, post = self.request('POST', '/api/tasks/t1/posts', {'body': '確認しました'})
        self.assertEqual(status, 201)
        self.assertEqual(post['classification'], 'expected')
        status, thread = self.request('GET', '/api/tasks/t1/thread')
        self.assertEqual(status, 200)
        self.assertEqual([item['body'] for item in thread['posts']], ['確認しました'])
        status, received = self.request('POST', f"/api/posts/{post['id']}/receive", {'received_by': 'uuid-worker-1'})
        self.assertEqual(status, 200)
        self.assertEqual(received['state'], 'received')
        self.assertEqual(self.request('POST', f"/api/posts/{post['id']}/receive", {})[0], 409)
        self.assertEqual(self.request('GET', '/api/tasks/missing/thread')[0], 404)
        self.assertEqual(self.request('POST', '/api/tasks/missing/posts', {'body': 'x'})[0], 404)

    def test_http_demo_には_board_の材料が入る(self):
        self.stop()
        self.start(demo=True)
        status, board = self.request('GET', '/api/board')
        self.assertEqual(status, 200)
        self.assertTrue(board['demo'])
        self.assertEqual(len(board['runs']), 1)
        self.assertEqual(len(board['sessions']), 2)
        self.assertEqual(len(board['posts']), 2)
        self.assertEqual(board['unread']['posts'], 2)
        status, thread = self.request('GET', '/api/tasks/demo-decision/thread')
        self.assertEqual(status, 200)
        self.assertEqual(thread['posts'][0]['classification'], 'expected')
        self.assertEqual(len(thread['messages']), 1)


if __name__ == '__main__':
    unittest.main()
