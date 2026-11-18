import http.client
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ADD_EXAMPLE, REQUEST_FIELDS, AppError, Store, make_server, object_fields
from test_app import isolated_env, request_payload

ROOT = Path(__file__).resolve().parents[1]


class SelfDescribeCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'cli.sqlite3'

    def cli(self, *arguments):
        return subprocess.run([sys.executable, str(ROOT / 'app.py'), '--db', str(self.db), *arguments], capture_output=True, text=True, timeout=10, env=isolated_env(self.temp.name))

    def write(self, value, name='payload.json'):
        path = Path(self.temp.name) / name
        path.write_text(json.dumps(value), encoding='utf-8')
        return path

    def rejected_error(self, payload):
        result = self.cli('add', '--file', str(self.write(payload)))
        self.assertEqual(result.returncode, 1, result.stdout)
        body = json.loads(result.stderr)
        self.assertEqual(set(body), {'error'})
        return body['error']

    def test_add_は_links_が文字列の配列なら項目名と型を_error_に並べる(self):
        error = self.rejected_error(request_payload(links=['https://example.com']))
        for expected in ('links[0]', 'url: string', 'kind: pr / issue / other', '受け取った型: string'):
            self.assertIn(expected, error)

    def test_add_は必須の欠落を全件示す(self):
        payload = request_payload()
        del payload['title'], payload['recommended']
        error = self.rejected_error(payload)
        self.assertIn('title は必須です (string)', error)
        self.assertIn('recommended は必須です (string)', error)

    def test_add_は未知の値と未知の項目を示す(self):
        payload = request_payload(priority='high', extra=1)
        payload['links'] = [{'url': 'https://example.com', 'label': 'x', 'kind': 'blog'}]
        error = self.rejected_error(payload)
        for expected in ('priority は urgent / normal / later のいずれか', 'links[0].kind は pr / issue / other のいずれか', 'extra は未知の項目です'):
            self.assertIn(expected, error)

    def test_add_は複数の不正を_1_本の_error_に全件並べる(self):
        error = self.rejected_error(request_payload(title=5, options='a', deadline=1, image=[]))
        for expected in ('title は string', 'options は [{', 'deadline は string | null', 'image は {url: string, alt: string} | null'):
            self.assertIn(expected, error)
        self.assertEqual(error.count('; '), 3)

    def test_add_は入れ子の不正を添字付きで示す(self):
        options = request_payload()['options']
        error = self.rejected_error(request_payload(options=[{**options[0], 'title': 3}, 'text']))
        self.assertIn('options[0].title は string', error)
        self.assertIn('options[1] は {id: string', error)

    def test_update_は未知の項目を示す(self):
        item = Store(self.db).add(request_payload())
        result = self.cli('update', '--id', item['id'], '--note', '未知', '--file', str(self.write({'bogus': 1}, 'changes.json')))
        self.assertEqual(result.returncode, 1)
        self.assertIn('bogus は未知の項目です', json.loads(result.stderr)['error'])

    def test_add_help_は表の全項目と通る例を出す(self):
        text = self.cli('add', '--help').stdout
        for key in REQUEST_FIELDS:
            self.assertIn(f'  {key}: ', text)
        self.assertIn('必須の項目:', text)
        marker = '最小の例 (そのまま add を通る):\n'
        self.assertIn(marker, text)
        example = json.loads(text.split(marker, 1)[1])
        self.assertEqual(example, ADD_EXAMPLE)
        added = self.cli('add', '--file', str(self.write(example, 'example.json')))
        self.assertEqual(added.returncode, 0, added.stderr)

    def test_list_help_と実際の出力の形が一致する(self):
        self.assertIn('{"decisions": [判断, ...], "demo": false}', self.cli('list', '--help').stdout)
        self.assertEqual(set(json.loads(self.cli('list').stdout)), {'decisions', 'demo'})

    def test_task_list_help_と実際の出力の形が一致する(self):
        self.assertIn('{"lanes": [{"id"', self.cli('task', 'list', '--help').stdout)
        data = json.loads(self.cli('task', 'list', '--json').stdout)
        self.assertEqual(set(data), {'lanes'})
        self.assertEqual(set(data['lanes'][0]), {'id', 'label', 'tasks'})


class SelfDescribeUnitTests(unittest.TestCase):
    def test_object_fields_は必須の欠落を示す(self):
        with self.assertRaises(AppError) as caught:
            object_fields({}, ('worker',), ('lease_seconds',))
        self.assertIn('worker は必須です', str(caught.exception))

    def test_表の全項目が_add_を通る(self):
        with tempfile.TemporaryDirectory() as temp:
            payload = request_payload(
                deadline='2030-01-01T00:00:00+09:00', request_key='run/1',
                files=[{'path': '/tmp/draft.md', 'label': '下書き'}],
                image={'url': 'https://example.com/a.png', 'alt': '図'})
            Store(Path(temp) / 'q.sqlite3').add(payload)
            self.assertEqual(set(payload) | {'priority'}, set(REQUEST_FIELDS))


class SelfDescribeHttpTests(unittest.TestCase):
    def test_post_は不正な_json_を_error_だけで_400_にする(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        server = make_server(Path(temp.name) / 'q.sqlite3', port=0, demo=False)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
        self.addCleanup(stop)
        connection = http.client.HTTPConnection('127.0.0.1', server.server_address[1], timeout=5)
        self.addCleanup(connection.close)
        connection.request('POST', '/api/decisions', body=json.dumps(request_payload(links=['https://example.com'], priority='high')).encode(), headers={'Content-Type': 'application/json'})
        response = connection.getresponse()
        body = json.loads(response.read())
        self.assertEqual(response.status, 400)
        self.assertEqual(set(body), {'error'})
        self.assertTrue(body['error'].startswith('JSON の項目を確認してください'))
        self.assertIn('links[0]', body['error'])
        self.assertIn('priority は urgent / normal / later のいずれか', body['error'])


if __name__ == '__main__':
    unittest.main()
