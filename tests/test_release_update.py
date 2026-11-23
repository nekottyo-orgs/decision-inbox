import fcntl
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
from threading import Thread
import unittest

from test_app import request_payload

ROOT = Path(__file__).resolve().parents[1]
UPDATER = ROOT / 'scripts/update-release.py'
FILES = (
    'app.py', 'cue_tui.py', 'assets/demo-flow.svg',
    'frontend/dist/index.html', 'frontend/dist/assets/app.js', 'frontend/dist/assets/app.css',
    'herdr-plugin/cue_plugin.py', 'herdr-plugin/herdr-plugin.toml',
)


class ReleaseUpdateTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.temp = Path(temporary.name)
        self.root = self.temp / 'cue'
        self.old = self.root / 'releases' / 'old'
        self.old.mkdir(parents=True)
        self.current = self.root / 'current'
        self.current.symlink_to('releases/old')
        self.cli = self.temp / 'decision-inbox'
        self.cli.symlink_to(self.current / 'app.py')
        self.db = self.temp / 'outside.sqlite3'
        self.payload = self.temp / 'payload'
        self.payload.mkdir()
        self.archive = self.temp / 'decision-inbox.tar.gz'
        self.checksum = self.temp / 'decision-inbox.tar.gz.sha256'
        for name in FILES:
            target = self.payload / name
            target.parent.mkdir(exist_ok=True, parents=True)
            if (ROOT / name).is_file():
                target.write_bytes((ROOT / name).read_bytes())
            else:
                target.write_text(name, encoding='utf-8')
        (self.payload / 'app.py').chmod(0o755)
        for name in FILES:
            target = self.old / name
            target.parent.mkdir(exist_ok=True, parents=True)
            target.write_bytes((self.payload / name).read_bytes())
        (self.old / 'app.py').chmod(0o755)
        subprocess.run([sys.executable, self.old / 'app.py', '--db', self.db, 'list'],
                       check=True, stdout=subprocess.DEVNULL)
        self.baseline = self.db.read_bytes()
        self.env = dict(os.environ, CUE_TEST_FIXTURE=str(self.temp))
        self.env['PATH'] = f"{self.temp}{os.pathsep}{self.env['PATH']}"
        self.stub('gh', '''
if sys.argv[1:3] == ['release', 'view']:
    print(json.dumps({'tagName': 'v0.0.1', 'isDraft': False}))
elif sys.argv[1] == 'api':
    print('a' * 40)
elif sys.argv[1:3] == ['release', 'download']:
    target = Path(sys.argv[sys.argv.index('--dir') + 1])
    for name in ('decision-inbox.tar.gz', 'decision-inbox.tar.gz.sha256'):
        shutil.copyfile(root / name, target / name)
else:
    sys.exit(5)
''')
        self.stub('herdr', '''
if sys.argv[1:3] == ['plugin', 'link']:
    destination = str(Path(sys.argv[3]).resolve())
    with (root / 'changes').open('a') as log:
        log.write(destination + '\\n')
    (root / 'plugin').write_text(destination)
else:
    plugin = {'plugin_id': 'nekottyo.cue', 'enabled': True,
              'plugin_root': (root / 'plugin').read_text()}
    print(json.dumps({'result': {'plugins': [plugin]}}))
''')
        self.stub('launchctl', r'''
if sys.argv[1] == 'print':
    script = ('/tmp/unrelated/app.py' if os.getenv('CUE_TEST_WRONG_SERVICE')
              else str(root / 'cue/current/app.py'))
    print('program = ' + sys.executable)
    print('state = running')
    print('pid = ' + (root / 'service-pid').read_text())
    print('arguments = {')
    for arg in (sys.executable, script, '--db', str(root / 'outside.sqlite3'),
                'serve', '--port', os.environ.get('CUE_TEST_PORT', '16317')):
        print('\t' + arg)
    print('}')
    sys.exit(0)
with (root / 'restarts').open('a') as log:
    log.write(' '.join(sys.argv[1:]) + '\n')
if os.getenv('CUE_TEST_FOREIGN_RESTART'):
    current = root / 'cue/current'
    current.unlink()
    current.symlink_to('releases/foreign')
if os.getenv('CUE_TEST_RESTART_FAIL') and (root / 'cue/current').resolve().name != 'old':
    sys.exit(2)
if not os.getenv('CUE_TEST_RESTART_NOOP') or (root / 'cue/current').resolve().name == 'old':
    pid = root / 'service-pid'
    pid.write_text(str(int(pid.read_text()) + 1))
''')
        (self.temp / 'service-pid').write_text('1000')
        (self.temp / 'plugin').write_text(str(self.old / 'herdr-plugin'))
        self.make_archive()

    def stub(self, name, code):
        stub = self.temp / name
        stub.write_text(
            f'#!{sys.executable}\nimport json, os, shutil, sys\nfrom pathlib import Path\n'
            'root = Path(os.environ["CUE_TEST_FIXTURE"])\n' + code, encoding='utf-8')
        stub.chmod(0o755)

    def make_archive(self, extra=None):
        with tarfile.open(self.archive, 'w:gz') as archive:
            for name in FILES:
                archive.add(self.payload / name, arcname=name)
            if extra:
                archive.add(self.payload / 'app.py', arcname=extra)
        self.checksum.write_text(
            hashlib.sha256(self.archive.read_bytes()).hexdigest()
            + '  decision-inbox.tar.gz\n', encoding='ascii')

    def start_web(self):
        current = self.current

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == '/api/decisions':
                    content = b'{"decisions": []}'
                else:
                    file = 'index.html' if self.path == '/' else self.path.lstrip('/')
                    content = (current / 'frontend/dist' / file).read_bytes()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(content)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.addCleanup(server.server_close)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        return server.server_port

    def update(self, port=16317):
        return subprocess.run([
            sys.executable, UPDATER, 'v0.0.1', '--root', self.root,
            '--cli', self.cli, '--db', self.db, '--service', 'com.example.cue', '--port', str(port),
        ], env=dict(self.env, CUE_TEST_PORT=str(port)), text=True, capture_output=True, timeout=45)

    def assert_preserved(self):
        self.assertEqual(os.readlink(self.current), 'releases/old')
        self.assertEqual(self.db.read_bytes(), self.baseline)

    def test_checksum_failure_keeps_current_and_does_not_relink(self):
        self.checksum.write_text('0' * 64 + '  decision-inbox.tar.gz\n', encoding='ascii')
        result = self.update()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('SHA-256', result.stderr)
        self.assert_preserved()
        self.assertFalse((self.temp / 'changes').exists())


    def test_success_and_retry_keep_backup_and_do_not_restart_twice(self):
        port = self.start_web()
        result = self.update(port)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.current.resolve().name, 'a' * 40)
        self.assertEqual(self.cli.resolve(), self.current.resolve() / 'app.py')
        self.assertEqual(self.db.read_bytes(), self.baseline)
        backups = list((self.root / 'backups').glob('*/decisions.sqlite3'))
        self.assertEqual(len(backups), 1)
        self.assertEqual((self.temp / 'plugin').read_text(),
                         str(self.current.resolve() / 'herdr-plugin'))
        self.assertEqual(len((self.temp / 'restarts').read_text().splitlines()), 1)
        result = self.update(port)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Already active', result.stdout)
        self.assertEqual(backups, list((self.root / 'backups').glob('*/decisions.sqlite3')))
        self.assertEqual(len((self.temp / 'restarts').read_text().splitlines()), 1)

    def test_restart_failure_restores_current_and_plugin(self):
        port = self.start_web()
        self.env['CUE_TEST_RESTART_FAIL'] = '1'
        result = self.update(port)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Restored previous current link', result.stderr)
        self.assert_preserved()
        self.assertEqual((self.temp / 'plugin').read_text(),
                         str(self.old.resolve() / 'herdr-plugin'))
        self.assertEqual(len((self.temp / 'restarts').read_text().splitlines()), 2)
        self.assertEqual(len(list((self.root / 'backups').glob('*/decisions.sqlite3'))), 1)

    def test_unexpected_archive_path_keeps_current(self):
        self.make_archive(extra='../extra')
        result = self.update()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Unexpected archive entry', result.stderr)
        self.assert_preserved()
        self.assertFalse((self.root / 'extra').exists())

    def test_incompatible_candidate_keeps_current_and_preserves_backup(self):
        (self.payload / 'app.py').write_text('raise SystemExit(1)\n', encoding='utf-8')
        self.make_archive()
        result = self.update()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('CLI could not open the DB copy', result.stderr)
        self.assert_preserved()
        self.assertFalse((self.temp / 'changes').exists())
        self.assertEqual(len(list((self.root / 'backups').glob('*/decisions.sqlite3'))), 1)

    def test_another_update_cannot_change_current(self):
        with (self.root / '.update-release.lock').open('a') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.update()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Another release update', result.stderr)
        self.assert_preserved()


    def test_candidate_must_show_existing_decisions(self):
        request = self.temp / 'request.json'
        request.write_text(json.dumps(request_payload()), encoding='utf-8')
        subprocess.run([sys.executable, self.old / 'app.py', '--db', self.db, 'add',
                        '--file', request], check=True, stdout=subprocess.DEVNULL)
        self.baseline = self.db.read_bytes()
        app = self.payload / 'app.py'
        app.write_text(app.read_text().replace(
            '    raise SystemExit(main())',
            '    if sys.argv[-2:] == ["list", "--include-withdrawn"]:\n'
            f"        print({json.dumps(dict(decisions=[], demo=False))!r})\n"
            '        raise SystemExit(0)\n'
            '    raise SystemExit(main())',
        ))
        self.make_archive()
        result = self.update(self.start_web())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Candidate did not preserve', result.stderr)
        self.assert_preserved()
        self.assertFalse((self.temp / 'changes').exists())

    def test_current_changed_during_restart_is_not_reported_successful(self):
        foreign = self.root / 'releases/foreign'
        import shutil
        shutil.copytree(self.old, foreign)
        self.env['CUE_TEST_FOREIGN_RESTART'] = '1'
        result = self.update(self.start_web())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('current changed outside this update', result.stderr)
        self.assertEqual(self.current.resolve(), foreign.resolve())
        self.assertEqual(len((self.temp / 'restarts').read_text().splitlines()), 1)

    def test_same_running_pid_after_restart_is_not_success(self):
        self.env['CUE_TEST_RESTART_NOOP'] = '1'
        result = self.update(self.start_web())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('LaunchAgent did not start a new process', result.stderr)
        self.assertIn('Restored previous current link', result.stderr)
        self.assert_preserved()

    def test_wrong_launch_agent_is_not_restarted(self):
        self.env['CUE_TEST_WRONG_SERVICE'] = '1'
        result = self.update(self.start_web())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('LaunchAgent', result.stderr)
        self.assert_preserved()
        self.assertFalse((self.temp / 'changes').exists())
        self.assertFalse((self.temp / 'restarts').exists())


if __name__ == '__main__':
    unittest.main()
