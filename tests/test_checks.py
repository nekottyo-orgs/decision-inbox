import contextlib
from datetime import datetime, timezone
import fcntl
import io
import json
import os
from pathlib import Path
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import (
    CHECK_FAULT_SKILL,
    Store,
    _terminate_process_group,
    check_list_data,
    check_lock_path,
    check_status_data,
    parse_check_jsonl,
    run_check_command,
    run_check_tick,
    run_wake_pass,
)
from test_app import isolated_env

ROOT = Path(__file__).resolve().parents[1]


def _git(repo, *args):
    result = subprocess.run(
        ['git', '-C', str(repo), *args],
        capture_output=True, text=True, check=True, timeout=10,
    )
    return result.stdout.strip()


def _write_exec(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return False


def _cleanup_pid_file(pid_file):
    if not pid_file.exists():
        return
    with contextlib.suppress(OSError, ValueError):
        pid = int(pid_file.read_text(encoding='utf-8').strip())
        if pid > 0 and _pid_alive(pid):
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.kill(pid, signal.SIGKILL)


class FakeClock:
    def __init__(self, wall=1_700_000_000.0, mono=1000.0):
        self.wall = float(wall)
        self.mono = float(mono)

    def now(self):
        return self.wall

    def monotonic(self):
        return self.mono

    def advance(self, seconds):
        self.wall += float(seconds)
        self.mono += float(seconds)


class CheckRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'cue.sqlite3'
        self.repo = self.root / 'agents-main'
        self.repo.mkdir()
        _git(self.repo, 'init', '-b', 'main')
        _git(self.repo, 'config', 'user.email', 'test@example.com')
        _git(self.repo, 'config', 'user.name', 'Test User')
        self.config = self.repo / 'checks.toml'
        self.wake_log = self.root / 'wake.jsonl'
        self.run_inbox = self.root / 'fake-run-inbox.sh'
        self._install_fake_run_inbox()

    def _install_fake_run_inbox(self, *, active_rc=0, send_rc=0, sleep_sec=0.0):
        script = f"""#!/usr/bin/env python3
import json, os, sys, time
log_path = {str(self.wake_log)!r}
cmd = sys.argv[1] if len(sys.argv) > 1 else ''
sleep_sec = float(os.environ.get('FAKE_WAKE_SLEEP', {str(sleep_sec)!r}))
if sleep_sec > 0:
    time.sleep(sleep_sec)
if cmd == 'active':
    rc = int(os.environ.get('FAKE_ACTIVE_RC', {str(active_rc)!r}))
    if rc != 0:
        sys.stderr.write('no active run\\n')
        sys.exit(rc)
    print('run-test-1')
    sys.exit(0)
if cmd == 'send':
    rc = int(os.environ.get('FAKE_SEND_RC', {str(send_rc)!r}))
    with open(log_path, 'a', encoding='utf-8') as f:
        f.write(json.dumps({{'argv': sys.argv[1:], 'rc': rc}}, ensure_ascii=False) + '\\n')
    if rc != 0:
        sys.stderr.write('send failed\\n')
    sys.exit(rc)
sys.exit(2)
"""
        _write_exec(self.run_inbox, script)

    def commit_config(self, toml_text, scripts=None):
        if scripts:
            for rel_path, body in scripts.items():
                _write_exec(self.repo / rel_path, body)
        self.config.write_text(toml_text, encoding='utf-8')
        _git(self.repo, 'add', '.')
        _git(self.repo, 'commit', '-m', 'update checks')

    def cli(self, *args, env_extra=None, timeout=15):
        env = isolated_env(self.root)
        env['CUE_RUN_INBOX_BIN'] = str(self.run_inbox)
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            [sys.executable, str(ROOT / 'app.py'), '--db', str(self.db), *args],
            capture_output=True, text=True, timeout=timeout, env=env,
        )

    def query_all(self, sql, params=()):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(sql, params)]

    def query_one(self, sql, params=()):
        rows = self.query_all(sql, params)
        return rows[0] if rows else None

    # --- 1. Schema & user_version=2 & readonly list/status ---

    def test_additive_tables_keep_user_version_2_and_readonly_commands_do_not_create_db(self):
        # DB が無い状態での check list / check status は DB を作らず正常終了
        res_list = self.cli('check', 'list', '--json')
        self.assertEqual(res_list.returncode, 0, res_list.stderr)
        self.assertEqual(json.loads(res_list.stdout), {'checks': []})
        res_status = self.cli('check', 'status', '--json')
        self.assertEqual(res_status.returncode, 0, res_status.stderr)
        self.assertIsNone(json.loads(res_status.stdout)['last_ok_tick_at'])
        self.assertFalse(self.db.exists())

        self.commit_config(
            '[[checks]]\nname = "c1"\ninterval = 300\ncommand = "scripts/ok.sh"\ntimeout = 30\nskill = "s1"\n',
            {'scripts/ok.sh': '#!/bin/sh\nexit 0\n'},
        )
        res = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000000'})
        self.assertEqual(res.returncode, 0, res.stderr)
        body = json.loads(res.stdout)
        self.assertEqual(body['status'], 'ok')

        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 2)
            self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone()[0].lower(), 'wal')
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for expected in ('tasks', 'checks', 'check_runs', 'check_run_events', 'check_ticks', 'check_tasks'):
                self.assertIn(expected, tables)
            # 既存の tasks テーブルの列は変更していない
            task_cols = {r[1] for r in conn.execute('PRAGMA table_info(tasks)')}
            self.assertEqual(
                task_cols,
                {'id', 'title', 'request', 'pane', 'run', 'result', 'remaining', 'lane', 'decision_id',
                 'created_at', 'updated_at', 'machine', 'kind', 'done_when'},
            )

    # --- 2. 同時 tick の skip (flock) と lockfile の inode 維持 ---

    def test_concurrent_tick_is_skipped_without_unlinking_lockfile(self):
        self.commit_config(
            '[[checks]]\nname = "c1"\ninterval = 300\ncommand = "scripts/ok.sh"\ntimeout = 30\nskill = "s1"\n',
            {'scripts/ok.sh': '#!/bin/sh\nexit 0\n'},
        )
        # 初回 tick でテーブルを初期化
        first = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000000'})
        self.assertEqual(first.returncode, 0, first.stderr)
        lock_file = check_lock_path(self.db)
        self.assertTrue(lock_file.exists())
        inode_before = lock_file.stat().st_ino

        # 別 fd で flock を保持したまま 2 回目の tick を実行 -> skipped になる
        fd = os.open(lock_file, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            second = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000500'})
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(json.loads(second.stdout)['status'], 'skipped')
        finally:
            os.close(fd)

        # lockfile は unlink されず同じ inode が維持される
        self.assertTrue(lock_file.exists())
        self.assertEqual(lock_file.stat().st_ino, inode_before)
        ticks = self.query_all('SELECT status, checks_deferred FROM check_ticks ORDER BY id')
        self.assertEqual([t['status'] for t in ticks], ['ok', 'skipped'])
        # skip は check の状態を変えない
        runs = self.query_all('SELECT * FROM check_runs')
        self.assertEqual(len(runs), 1)

    def test_skip_when_db_not_initialized_does_not_create_db_or_block(self):
        lock_file = check_lock_path(self.db)
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            res = self.cli('check', 'tick', '--config', str(self.config))
            self.assertEqual(res.returncode, 0)
            self.assertEqual(json.loads(res.stdout)['status'], 'skipped')
            self.assertIn('skipped', res.stderr)
            # ロック未取得経路では DB ファイルも DDL も作らない
            self.assertFalse(self.db.exists())
        finally:
            os.close(fd)

    def test_symlink_db_path_shares_lockfile_with_real_path(self):
        self.commit_config(
            '[[checks]]\nname = "c1"\ninterval = 300\ncommand = "scripts/ok.sh"\ntimeout = 30\nskill = "s1"\n',
            {'scripts/ok.sh': '#!/bin/sh\nexit 0\n'},
        )
        # 初回 tick で実体 DB と lockfile を作成
        first = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000000'})
        self.assertEqual(first.returncode, 0, first.stderr)

        # (a) ファイル symlink と (b) ディレクトリ symlink の両方で lockfile が実体パスと一致すること
        link_db = self.root / 'link.sqlite3'
        link_db.symlink_to(self.db)
        real_dir = self.root / 'real_dir'
        real_dir.mkdir()
        real_db2 = real_dir / 'cue2.sqlite3'
        link_dir = self.root / 'link_dir'
        link_dir.symlink_to(real_dir)
        link_db2 = link_dir / 'cue2.sqlite3'

        self.assertEqual(check_lock_path(link_db), check_lock_path(self.db))
        self.assertEqual(check_lock_path(link_db2), check_lock_path(real_db2))

        # 実体パス側で flock 保持中、symlink 経由の 2 本目 tick は skipped になる
        real_lock = check_lock_path(self.db)
        fd = os.open(real_lock, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            res_sym, rc_sym = run_check_tick(
                link_db, self.config,
                now_fn=lambda: 1700000500.0,
                deliver_fn=lambda c, t: True,
            )
            self.assertEqual(rc_sym, 0)
            self.assertEqual(res_sym['status'], 'skipped')
        finally:
            os.close(fd)

        # symlink 側に別の lockfile が作られていないこと、skip が実体 DB に記録されていること
        self.assertFalse((self.root / 'link.sqlite3.check.lock').exists())
        ticks = self.query_all('SELECT status FROM check_ticks ORDER BY id')
        self.assertEqual([t['status'] for t in ticks], ['ok', 'skipped'])

        # 逆方向: ディレクトリ symlink 経由で flock 保持中、実体パス経由の tick も skipped になる
        res_init, rc_init = run_check_tick(
            link_db2, self.config,
            now_fn=lambda: 1700000000.0,
            deliver_fn=lambda c, t: True,
        )
        self.assertEqual((rc_init, res_init['status']), (0, 'ok'))
        lock2 = check_lock_path(link_db2)
        fd2 = os.open(lock2, os.O_RDWR)
        try:
            fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
            res_real, rc_real = run_check_tick(
                real_db2, self.config,
                now_fn=lambda: 1700000500.0,
                deliver_fn=lambda c, t: True,
            )
            self.assertEqual(rc_real, 0)
            self.assertEqual(res_real['status'], 'skipped')
        finally:
            os.close(fd2)

    # --- 3. kill -9 後の running 残留と次 tick の継続 ---

    def test_kill_9_leaves_running_tick_and_next_tick_recovers(self):
        marker = self.root / 'started.flag'
        self.commit_config(
            '[[checks]]\nname = "slow"\ninterval = 300\ncommand = "scripts/slow.sh"\ntimeout = 30\nskill = "s1"\n',
            {'scripts/slow.sh': f'#!/bin/sh\ntouch {str(marker)!r}\nsleep 20\n'},
        )
        env = isolated_env(self.root)
        env['CUE_RUN_INBOX_BIN'] = str(self.run_inbox)
        env['CUE_CHECK_NOW'] = '1700000000'
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / 'app.py'), '--db', str(self.db), 'check', 'tick', '--config', str(self.config)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 10
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(marker.exists(), 'slow check did not start')
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)
        finally:
            with contextlib.suppress(ProcessLookupError, OSError):
                os.killpg(proc.pid, signal.SIGKILL)
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()

        # running 行が残留している
        ticks = self.query_all('SELECT status FROM check_ticks ORDER BY id')
        self.assertEqual([t['status'] for t in ticks], ['running'])

        # 軽い script に切り替えて次 tick を回すと OS が flock 解放済みなので継続できる
        self.commit_config(
            '[[checks]]\nname = "slow"\ninterval = 300\ncommand = "scripts/fast.sh"\ntimeout = 30\nskill = "s1"\n',
            {'scripts/fast.sh': '#!/bin/sh\nexit 0\n'},
        )
        nxt = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000300'})
        self.assertEqual(nxt.returncode, 0, nxt.stderr)
        self.assertEqual(json.loads(nxt.stdout)['status'], 'ok')

        # check status が running 残留を警告する
        st = self.cli('check', 'status')
        self.assertEqual(st.returncode, 0)
        self.assertIn('WARNING', st.stdout)
        self.assertIn('running 残留', st.stdout)
        st_json = json.loads(self.cli('check', 'status', '--json').stdout)
        self.assertEqual(st_json['running_leftover_count'], 1)
        self.assertEqual([t['status'] for t in st_json['recent_ticks']], ['running', 'ok'])

    # --- 4. checkout 検査と TOML 検証 (config_error) ---

    def test_checkout_and_toml_validation_rules(self):
        self.commit_config(
            '[[checks]]\nname = "c1"\ninterval = 300\ncommand = "scripts/ok.sh"\ntimeout = 30\nskill = "s1"\n',
            {'scripts/ok.sh': '#!/bin/sh\nexit 0\n'},
        )
        # untracked ファイル (.DS_Store) と未 push のローカル commit は通過する
        (self.repo / '.DS_Store').write_text('x', encoding='utf-8')
        ok_res = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000000'})
        self.assertEqual(ok_res.returncode, 0, ok_res.stderr)

        # tracked ファイルの未コミット変更 -> config_error (exit 1, checks_deferred=NULL)
        (self.repo / 'scripts/ok.sh').write_text('#!/bin/sh\n# dirty\nexit 0\n', encoding='utf-8')
        dirty = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000300'})
        self.assertEqual(dirty.returncode, 1)
        dirty_json = json.loads(dirty.stdout)
        self.assertEqual(dirty_json['status'], 'config_error')
        self.assertIsNone(dirty_json['checks_deferred'])
        _git(self.repo, 'checkout', '--', 'scripts/ok.sh')

        # 別ブランチ -> config_error
        _git(self.repo, 'checkout', '-b', 'feature/x')
        br = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000600'})
        self.assertEqual(br.returncode, 1)
        self.assertEqual(json.loads(br.stdout)['status'], 'config_error')
        _git(self.repo, 'checkout', 'main')

        # symlink 脱出 -> config_error
        outside = self.root / 'outside.sh'
        _write_exec(outside, '#!/bin/sh\nexit 0\n')
        esc = self.repo / 'scripts/escape.sh'
        esc.symlink_to(outside)
        self.commit_config(
            '[[checks]]\nname = "good"\ninterval = 300\ncommand = "scripts/ok.sh"\ntimeout = 30\nskill = "s1"\n'
            '[[checks]]\nname = "bad"\ninterval = 300\ncommand = "scripts/escape.sh"\ntimeout = 30\nskill = "s1"\n',
        )
        sym_res = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000900'})
        self.assertEqual(sym_res.returncode, 1)
        self.assertEqual(json.loads(sym_res.stdout)['status'], 'config_error')
        # 部分適用されない ("good" は checks テーブルに追加されていない)
        names = [r['name'] for r in self.query_all('SELECT name FROM checks')]
        self.assertEqual(names, ['c1'])

        # 各不正 TOML パターン (timeout > 180, interval <= 0, duplicate name, empty skill, fault_threshold < 1, .. 脱出, env キー不正)
        bad_tomls = [
            '[[checks]]\nname = "a"\ninterval = 300\ncommand = "scripts/ok.sh"\ntimeout = 181\nskill = "s"\n',
            '[[checks]]\nname = "a"\ninterval = 0\ncommand = "scripts/ok.sh"\ntimeout = 10\nskill = "s"\n',
            '[[checks]]\nname = "a"\ninterval = 300\ncommand = "scripts/ok.sh"\ntimeout = 10\nskill = ""\n',
            '[[checks]]\nname = "a"\ninterval = 300\ncommand = "scripts/ok.sh"\ntimeout = 10\nskill = "s"\nfault_threshold = 0\n',
            '[[checks]]\nname = "a"\ninterval = 300\ncommand = "../outside.sh"\ntimeout = 10\nskill = "s"\n',
            '[[checks]]\nname = "dup"\ninterval = 300\ncommand = "scripts/ok.sh"\ntimeout = 10\nskill = "s"\n'
            '[[checks]]\nname = "dup"\ninterval = 300\ncommand = "scripts/ok.sh"\ntimeout = 10\nskill = "s"\n',
            '[[checks]]\nname = "a"\ninterval = 300\ncommand = "scripts/ok.sh"\ntimeout = 10\nskill = "s"\n[checks.env]\n"BAD=KEY" = "1"\n',
            '[[checks]]\nname = "a"\ninterval = 300\ncommand = "scripts/ok.sh"\ntimeout = 10\nskill = "s"\n[checks.env]\n"" = "1"\n',
            'not valid toml === [',
        ]
        for idx, bad in enumerate(bad_tomls):
            self.commit_config(bad)
            r = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': str(1700001200 + idx * 10)})
            self.assertEqual(r.returncode, 1, f'case {idx} should fail')
            self.assertEqual(json.loads(r.stdout)['status'], 'config_error', f'case {idx}')

    def test_popen_value_error_is_recorded_as_check_failure_not_running_crash(self):
        self.commit_config(
            '[[checks]]\nname = "c1"\ninterval = 300\ncommand = "scripts/ok.sh"\ntimeout = 30\nskill = "s1"\n',
            {'scripts/ok.sh': '#!/bin/sh\nexit 0\n'},
        )
        # 1. run_check_command 直接呼び出しで不正 env (Popen ValueError) が例外死せず失敗として返ること
        chk = {
            'name': 'c1',
            'argv': [str(self.repo / 'scripts/ok.sh')],
            'timeout': 10.0,
            'env': {'BAD=KEY': '1'},
        }
        rc, out, err, timed_out, aborted = run_check_command(chk, self.repo, time.monotonic() + 30.0)
        self.assertIsNone(rc)
        self.assertEqual((out, timed_out, aborted), (b'', False, False))
        self.assertTrue(len(err) > 0)

        # 2. tick 内で check 起動時の Popen が ValueError を投げても running 残留せず failed_exit として記録されること
        real_popen = subprocess.Popen

        def popen_failing_on_check(argv, *args, **kwargs):
            if argv and str(argv[0]).endswith('scripts/ok.sh'):
                raise ValueError('illegal environment variable name')
            return real_popen(argv, *args, **kwargs)

        with mock.patch('app.subprocess.Popen', side_effect=popen_failing_on_check):
            res, exit_code = run_check_tick(
                self.db, self.config,
                now_fn=lambda: 1700000000.0,
                deliver_fn=lambda c, t: True,
            )
        self.assertEqual((exit_code, res['status']), (0, 'ok'))
        run = self.query_one('SELECT outcome, exit_code, stderr_excerpt FROM check_runs')
        self.assertEqual(run['outcome'], 'failed_exit')
        self.assertIsNone(run['exit_code'])
        self.assertIn('illegal environment variable name', run['stderr_excerpt'])
        self.assertEqual(self.query_all("SELECT * FROM check_ticks WHERE status='running'"), [])

    def test_config_error_still_runs_start_wake_before_failing(self):
        self.commit_config(
            '[[checks]]\nname = "c1"\ninterval = 300\ncommand = "scripts/fire.sh"\ntimeout = 30\nskill = "s1"\n',
            {'scripts/fire.sh': '#!/bin/sh\necho \'{"key":"k1","title":"t1","body":"b1"}\'\n'},
        )
        # 1 回目: wake を失敗させて未通知タスクを残す
        r1 = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000000', 'FAKE_SEND_RC': '1'})
        self.assertEqual(r1.returncode, 0)
        self.assertEqual(json.loads(r1.stdout)['unnotified_tasks'], 1)
        self.assertEqual(json.loads(r1.stdout)['wake_failed'], 1)

        # 2 回目: config を壊した状態で wake 成功 -> 開始時 wake が走って未通知タスクが 0 になり、tick は config_error
        self.commit_config('not valid = [')
        r2 = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000300', 'FAKE_SEND_RC': '0'})
        self.assertEqual(r2.returncode, 1)
        b2 = json.loads(r2.stdout)
        self.assertEqual(b2['status'], 'config_error')
        self.assertEqual(b2['unnotified_tasks'], 0)
        self.assertEqual(b2['wake_failed'], 0)
        self.assertIsNone(b2['checks_deferred'])

    # --- 5. due 判定・基準時刻切替・時刻巻き戻り・順序 ---

    def test_due_base_switch_between_success_and_failure_and_clock_rollback(self):
        self.commit_config(
            '[[checks]]\nname = "c1"\ninterval = 300\ncommand = "scripts/c1.sh"\ntimeout = 30\nskill = "s1"\n',
            {'scripts/c1.sh': '#!/bin/sh\nexit 0\n'},
        )
        clock = FakeClock(wall=1000.0, mono=100.0)
        # 初回 (履歴なし): 即 due。tick_started=1000 で成功
        res, rc = run_check_tick(self.db, self.config, now_fn=clock.now, mono_fn=clock.monotonic, deliver_fn=lambda c, t: True)
        self.assertEqual((rc, res['status']), (0, 'ok'))
        self.assertEqual(len(self.query_all('SELECT * FROM check_runs')), 1)

        # grace = min(30, 300*0.1) = 30 -> 1000 + 300 - 30 = 1270 未満は due でない
        clock.wall = 1269.0
        run_check_tick(self.db, self.config, now_fn=clock.now, mono_fn=clock.monotonic, deliver_fn=lambda c, t: True)
        self.assertEqual(len(self.query_all('SELECT * FROM check_runs')), 1)

        # 1270 ちょうどで due
        clock.wall = 1270.0
        run_check_tick(self.db, self.config, now_fn=clock.now, mono_fn=clock.monotonic, deliver_fn=lambda c, t: True)
        self.assertEqual(len(self.query_all('SELECT * FROM check_runs')), 2)

        # 失敗系 (actual_started_at = tick_started + 40 となるよう設定)
        self.commit_config(
            '[[checks]]\nname = "c1"\ninterval = 300\ncommand = "scripts/fail.sh"\ntimeout = 30\nskill = "s1"\n',
            {'scripts/fail.sh': '#!/bin/sh\nexit 1\n'},
        )
        # 2000 で tick 開始、actual_started_at を 2040 相当にするため tick 中に wall を進める
        calls = {'n': 0}

        def shifting_now():
            calls['n'] += 1
            # 1 回目が tick_started_ts、以降 (actual_started 等) は +40 秒
            return 2000.0 if calls['n'] == 1 else 2040.0

        run_check_tick(self.db, self.config, now_fn=shifting_now, mono_fn=clock.monotonic, deliver_fn=lambda c, t: True)
        self.assertEqual(len(self.query_all('SELECT * FROM check_runs')), 3)
        chk = self.query_one("SELECT * FROM checks WHERE name='c1'")
        self.assertEqual(chk['last_outcome'], 'failed_exit')

        # 失敗後は last_actual_started_at (2040) 基準 -> 2040 + 300 - 30 = 2310。2300 では due でない!
        clock.wall = 2300.0
        run_check_tick(self.db, self.config, now_fn=clock.now, mono_fn=clock.monotonic, deliver_fn=lambda c, t: True)
        self.assertEqual(len(self.query_all('SELECT * FROM check_runs')), 3)

        # 2310 で due
        clock.wall = 2310.0
        run_check_tick(self.db, self.config, now_fn=clock.now, mono_fn=clock.monotonic, deliver_fn=lambda c, t: True)
        self.assertEqual(len(self.query_all('SELECT * FROM check_runs')), 4)

        # 時刻巻き戻り (tick_started = 1900 < last_started = 2310) -> 即 due かつ clock_anomaly = 1
        clock.wall = 1900.0
        run_check_tick(self.db, self.config, now_fn=clock.now, mono_fn=clock.monotonic, deliver_fn=lambda c, t: True)
        runs = self.query_all('SELECT outcome, clock_anomaly FROM check_runs ORDER BY id')
        self.assertEqual(len(runs), 5)
        self.assertEqual(runs[-1]['clock_anomaly'], 1)

    def test_due_ordering_no_history_then_clock_anomaly_then_oldest(self):
        order_log = self.root / 'order.log'
        self.commit_config(
            f'[[checks]]\nname = "old_normal"\ninterval = 60\ncommand = "scripts/rec.sh"\ntimeout = 10\nskill = "s"\nenv = {{NAME = "old_normal"}}\n'
            f'[[checks]]\nname = "rollback"\ninterval = 60\ncommand = "scripts/rec.sh"\ntimeout = 10\nskill = "s"\nenv = {{NAME = "rollback"}}\n'
            f'[[checks]]\nname = "fresh"\ninterval = 60\ncommand = "scripts/rec.sh"\ntimeout = 10\nskill = "s"\nenv = {{NAME = "fresh"}}\n',
            {'scripts/rec.sh': f'#!/bin/sh\necho "$NAME" >> {str(order_log)!r}\n'},
        )
        # 初期登録だけ済ませて手動で履歴を配置
        res = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000000'})
        self.assertEqual(res.returncode, 0)
        order_log.unlink()

        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE checks SET last_started=NULL, last_actual_started_at=NULL, last_outcome=NULL WHERE name='fresh'")
            conn.execute(
                "UPDATE checks SET last_started=?, last_actual_started_at=?, last_outcome='ok_silent' WHERE name='rollback'",
                (datetime.fromtimestamp(1700005000, timezone.utc).isoformat(timespec='microseconds'),
                 datetime.fromtimestamp(1700005000, timezone.utc).isoformat(timespec='microseconds')),
            )
            conn.execute(
                "UPDATE checks SET last_started=?, last_actual_started_at=?, last_outcome='ok_silent' WHERE name='old_normal'",
                (datetime.fromtimestamp(1700000100, timezone.utc).isoformat(timespec='microseconds'),
                 datetime.fromtimestamp(1700000100, timezone.utc).isoformat(timespec='microseconds')),
            )

        res2 = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700001000'})
        self.assertEqual(res2.returncode, 0)
        executed = order_log.read_text(encoding='utf-8').splitlines()
        self.assertEqual(executed, ['fresh', 'rollback', 'old_normal'])

    # --- 6. tick 上限・先頭免除・2 件目以降ガード・aborted / failed_timeout・プロセスグループ終了 ---

    def test_first_check_exemption_aborted_does_not_mutate_check_state(self):
        self.commit_config(
            '[[checks]]\nname = "c1"\ninterval = 300\ncommand = "scripts/sleep.sh"\ntimeout = 10\nskill = "s1"\n'
            '[[checks]]\nname = "c2"\ninterval = 300\ncommand = "scripts/ok.sh"\ntimeout = 10\nskill = "s1"\n',
            {
                'scripts/sleep.sh': '#!/bin/sh\nsleep 5\n',
                'scripts/ok.sh': '#!/bin/sh\nexit 0\n',
            },
        )
        # まず c1 を 1 回失敗させて fault_count=1, last_outcome='failed_exit' を作っておく
        self.commit_config(
            '[[checks]]\nname = "c1"\ninterval = 60\ncommand = "scripts/fail_or_sleep.sh"\ntimeout = 10\nskill = "s1"\n'
            '[[checks]]\nname = "c2"\ninterval = 60\ncommand = "scripts/ok.sh"\ntimeout = 10\nskill = "s1"\n',
            {
                'scripts/fail_or_sleep.sh': '#!/bin/sh\nif [ "${MODE:-}" = sleep ]; then sleep 5; else exit 1; fi\n',
                'scripts/ok.sh': '#!/bin/sh\nexit 0\n',
            },
        )
        r0 = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000000'})
        self.assertEqual(r0.returncode, 0)
        before_c1 = self.query_one("SELECT last_started, last_actual_started_at, last_outcome, fault_count FROM checks WHERE name='c1'")
        self.assertEqual((before_c1['last_outcome'], before_c1['fault_count']), ('failed_exit', 1))

        # tick_budget = 0.6s (< c1.timeout=10s) -> 先頭 c1 は免除で着手し tick 上限で aborted。c2 は未着手で checks_deferred=1
        # setup (git / SQLite) の所要時間に依存しないよう、最初の check 起動時から経過時間を計る
        real_origin = [None]

        def mono_fn():
            if real_origin[0] is None:
                return 0.0
            return time.monotonic() - real_origin[0]

        def start_clock_on_first_check(*args, **kwargs):
            if real_origin[0] is None:
                real_origin[0] = time.monotonic()
            return run_check_command(*args, **kwargs)

        with mock.patch.dict(os.environ, {'MODE': 'sleep'}), mock.patch(
            'app.run_check_command', side_effect=start_clock_on_first_check,
        ):
            b1, rc1 = run_check_tick(
                self.db, self.config,
                now_fn=lambda: 1700001000.0,
                mono_fn=mono_fn,
                tick_budget=0.6,
                kill_grace=0.2,
                deliver_fn=lambda c, t: True,
            )
        self.assertEqual(rc1, 0)
        self.assertEqual(b1['status'], 'aborted')
        self.assertEqual(b1['checks_deferred'], 1)

        # check_runs に aborted が記録され、checks テーブルの実行状態カラムは一切変わらない!
        after_c1 = self.query_one("SELECT last_started, last_actual_started_at, last_outcome, fault_count FROM checks WHERE name='c1'")
        self.assertEqual(after_c1, before_c1)
        last_run = self.query_one("SELECT outcome FROM check_runs WHERE check_name='c1' ORDER BY id DESC LIMIT 1")
        self.assertEqual(last_run['outcome'], 'aborted')

    def test_step6_remaining_zero_aborts_with_zero_runs_and_all_deferred(self):
        self.commit_config(
            '[[checks]]\nname = "c1"\ninterval = 60\ncommand = "scripts/ok.sh"\ntimeout = 10\nskill = "s1"\n'
            '[[checks]]\nname = "c2"\ninterval = 60\ncommand = "scripts/ok.sh"\ntimeout = 10\nskill = "s1"\n',
            {'scripts/ok.sh': '#!/bin/sh\nexit 0\n'},
        )
        clock = FakeClock(wall=1700000000.0, mono=1000.0)
        body, rc = run_check_tick(
            self.db, self.config,
            now_fn=clock.now,
            mono_fn=clock.monotonic,
            tick_budget=0.0,
            deliver_fn=lambda c, t: True,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(body['status'], 'aborted')
        self.assertEqual(body['checks_deferred'], 2)
        self.assertEqual(self.query_all('SELECT * FROM check_runs'), [])

    def test_second_check_guard_skips_heavy_and_runs_light_check_with_status_ok(self):
        self.commit_config(
            '[[checks]]\nname = "c1"\ninterval = 60\ncommand = "scripts/fast.sh"\ntimeout = 1\nskill = "s1"\n'
            '[[checks]]\nname = "c2_heavy"\ninterval = 60\ncommand = "scripts/fast.sh"\ntimeout = 50\nskill = "s1"\n'
            '[[checks]]\nname = "c3_light"\ninterval = 60\ncommand = "scripts/fast.sh"\ntimeout = 1\nskill = "s1"\n',
            {'scripts/fast.sh': '#!/bin/sh\nexit 0\n'},
        )
        # tick_budget = 5s: c1 (timeout 1) 実行 -> c2_heavy (timeout 50 > remaining) 見送り -> c3_light (timeout 1) 実行!
        clock = FakeClock(wall=1700000000.0, mono=1000.0)
        body, rc = run_check_tick(
            self.db, self.config,
            now_fn=clock.now,
            mono_fn=clock.monotonic,
            tick_budget=5.0,
            deliver_fn=lambda c, t: True,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(body['status'], 'ok')
        self.assertEqual(body['checks_deferred'], 1)
        ran = [r['check_name'] for r in self.query_all('SELECT check_name FROM check_runs ORDER BY id')]
        self.assertEqual(ran, ['c1', 'c3_light'])

    def test_checks_deferred_sums_guard_skip_and_post_abort_unstarted(self):
        self.commit_config(
            '[[checks]]\nname = "c1_fast"\ninterval = 60\ncommand = "scripts/fast.sh"\ntimeout = 1\nskill = "s1"\n'
            '[[checks]]\nname = "c2_guard_skip"\ninterval = 60\ncommand = "scripts/fast.sh"\ntimeout = 50\nskill = "s1"\n'
            '[[checks]]\nname = "c3_aborted"\ninterval = 60\ncommand = "scripts/slow.sh"\ntimeout = 1\nskill = "s1"\n'
            '[[checks]]\nname = "c4_unstarted"\ninterval = 60\ncommand = "scripts/fast.sh"\ntimeout = 1\nskill = "s1"\n'
            '[[checks]]\nname = "c5_unstarted"\ninterval = 60\ncommand = "scripts/fast.sh"\ntimeout = 1\nskill = "s1"\n',
            {
                'scripts/fast.sh': '#!/bin/sh\nexit 0\n',
                'scripts/slow.sh': '#!/bin/sh\nsleep 5\n',
            },
        )
        # c1_fast 完了後に c2_guard_skip が着手前ガードで見送り (+1)、
        # c3_aborted はガード通過後に残り時間が縮んで tick 上限で aborted、
        # 残りの c4_unstarted / c5_unstarted (+2) と合算されて checks_deferred == 3 になること
        # setup 時間で c1_fast / c3_aborted の前提が崩れないよう、c3_aborted 着手時から時計を進める
        c3_real_origin = [None]

        def mono_fn():
            if c3_real_origin[0] is None:
                return 0.0
            return 1.6 + (time.monotonic() - c3_real_origin[0])

        def wrapped_run_check(chk, *args, **kwargs):
            if chk['name'] == 'c3_aborted':
                c3_real_origin[0] = time.monotonic()
            return run_check_command(chk, *args, **kwargs)

        with mock.patch('app.run_check_command', side_effect=wrapped_run_check):
            body, rc = run_check_tick(
                self.db, self.config,
                now_fn=lambda: 1700000000.0,
                mono_fn=mono_fn,
                tick_budget=2.0,
                kill_grace=0.1,
                deliver_fn=lambda c, t: True,
            )
        self.assertEqual(rc, 0)
        self.assertEqual(body['status'], 'aborted')
        self.assertEqual(body['checks_deferred'], 3)
        runs = [(r['check_name'], r['outcome']) for r in self.query_all('SELECT check_name, outcome FROM check_runs ORDER BY id')]
        self.assertEqual(runs, [('c1_fast', 'ok_silent'), ('c3_aborted', 'aborted')])
        tick_row = self.query_one('SELECT status, checks_deferred FROM check_ticks ORDER BY id DESC LIMIT 1')
        self.assertEqual((tick_row['status'], tick_row['checks_deferred']), ('aborted', 3))

    def test_deadline_crossed_after_guard_aborts_without_spawning_check(self):
        ran_marker = self.root / 'should_not_run.marker'
        self.commit_config(
            '[[checks]]\nname = "c1"\ninterval = 60\ncommand = "scripts/mark.sh"\ntimeout = 10\nskill = "s1"\n'
            '[[checks]]\nname = "c2"\ninterval = 60\ncommand = "scripts/mark.sh"\ntimeout = 10\nskill = "s1"\n',
            {
                'scripts/mark.sh': f'#!/bin/sh\necho ran > {str(ran_marker)!r}\nexit 0\n',
            },
        )
        clock = FakeClock(wall=1700000000.0, mono=1000.0)
        spawned_commands = []
        orig_popen = subprocess.Popen

        def spy_popen(argv, *args, **kwargs):
            spawned_commands.append(list(argv))
            return orig_popen(argv, *args, **kwargs)

        def advance_past_deadline_after_guard(chk, *args, **kwargs):
            # budget guard を通過した後、run_check_command の Popen 直前で deadline を跨ぐ
            clock.advance(1.5)
            return run_check_command(chk, *args, **kwargs)

        with mock.patch('app.subprocess.Popen', side_effect=spy_popen), mock.patch(
            'app.run_check_command', side_effect=advance_past_deadline_after_guard,
        ):
            body, rc = run_check_tick(
                self.db, self.config,
                now_fn=clock.now,
                mono_fn=clock.monotonic,
                tick_budget=1.0,
                deliver_fn=lambda c, t: True,
            )

        self.assertEqual(rc, 0)
        self.assertEqual(body['status'], 'aborted')
        self.assertEqual(body['checks_deferred'], 1)
        self.assertFalse(ran_marker.exists())
        self.assertFalse(any(any('mark.sh' in part for part in cmd) for cmd in spawned_commands))
        runs = [(r['check_name'], r['outcome'], r['exit_code']) for r in self.query_all('SELECT check_name, outcome, exit_code FROM check_runs')]
        self.assertEqual(runs, [('c1', 'aborted', None)])
        c1_state = self.query_one("SELECT last_started, last_actual_started_at, last_outcome, fault_count FROM checks WHERE name='c1'")
        self.assertEqual(
            (c1_state['last_started'], c1_state['last_actual_started_at'], c1_state['last_outcome'], c1_state['fault_count']),
            (None, None, None, 0),
        )

    def test_cli_ignores_fixed_budget_override_env_vars(self):
        self.commit_config(
            '[[checks]]\nname = "c1"\ninterval = 60\ncommand = "scripts/ok.sh"\ntimeout = 10\nskill = "s1"\n',
            {'scripts/ok.sh': '#!/bin/sh\nexit 0\n'},
        )
        # 設計上固定の上限を環境変数で 0 に書き換えようとしても本番 CLI は無視し、通常通り実行される
        res = self.cli(
            'check', 'tick', '--config', str(self.config),
            env_extra={
                'CUE_CHECK_NOW': '1700000000',
                'CUE_CHECK_TICK_BUDGET_SEC': '0',
                'CUE_CHECK_WAKE_BUDGET_SEC': '0',
                'CUE_CHECK_WAKE_ITEM_TIMEOUT_SEC': '999',
                'CUE_CHECK_KILL_GRACE_SEC': '0',
            },
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        body = json.loads(res.stdout)
        self.assertEqual(body['status'], 'ok')
        self.assertEqual(body['checks_deferred'], 0)
        runs = self.query_all('SELECT check_name, outcome FROM check_runs')
        self.assertEqual(runs, [{'check_name': 'c1', 'outcome': 'ok_silent'}])

    def test_timeout_priority_and_process_group_sigterm_then_sigkill(self):
        child_pid_file = self.root / 'child.pid'
        self.addCleanup(_cleanup_pid_file, child_pid_file)
        self.commit_config(
            '[[checks]]\nname = "hang"\ninterval = 60\ncommand = "scripts/hang.sh"\ntimeout = 0.5\nskill = "s1"\n',
            {
                'scripts/hang.sh': (
                    '#!/bin/sh\n'
                    'trap "" TERM\n'
                    f'python3 -c \'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)\' &\n'
                    f'echo $! > {str(child_pid_file)!r}\n'
                    'wait\n'
                ),
            },
        )
        body, rc = run_check_tick(
            self.db, self.config,
            now_fn=lambda: 1700000000.0,
            kill_grace=0.2,
            deliver_fn=lambda c, t: True,
        )
        self.assertEqual((rc, body['status']), (0, 'ok'))
        run = self.query_one('SELECT outcome, exit_code FROM check_runs')
        self.assertEqual(run['outcome'], 'failed_timeout')
        self.assertIsNone(run['exit_code'])
        # 孫プロセスも SIGKILL で確実に消えていること
        child_pid = int(child_pid_file.read_text(encoding='utf-8').strip())
        time.sleep(0.1)
        with self.assertRaises(ProcessLookupError):
            os.kill(child_pid, 0)

    def test_timeout_sigkills_term_ignoring_child_after_leader_exits_on_term(self):
        child_pid_file = self.root / 'child_orphan.pid'
        self.addCleanup(_cleanup_pid_file, child_pid_file)
        child_ready_flag = self.root / 'child_ready.flag'
        leader_exited_flag = self.root / 'leader_exited.flag'
        child_code = (
            'import signal, time; '
            'signal.signal(signal.SIGTERM, signal.SIG_IGN); '
            f'open({str(child_ready_flag)!r}, "w", encoding="utf-8").write("ready"); '
            'time.sleep(60)'
        )
        leader_script = (
            '#!/usr/bin/env python3\n'
            'import os, signal, subprocess, sys, time\n'
            'child = subprocess.Popen(\n'
            f'    [sys.executable, "-c", {child_code!r}],\n'
            '    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,\n'
            ')\n'
            f'open({str(child_pid_file)!r}, "w", encoding="utf-8").write(str(child.pid))\n'
            'def on_term(_signum, _frame):\n'
            f'    open({str(leader_exited_flag)!r}, "w", encoding="utf-8").write("term")\n'
            '    os._exit(0)\n'
            'signal.signal(signal.SIGTERM, on_term)\n'
            'time.sleep(60)\n'
        )
        self.commit_config(
            '[[checks]]\nname = "leader_exits"\ninterval = 60\ncommand = "scripts/leader_exits.py"\ntimeout = 1.0\nskill = "s1"\n',
            {'scripts/leader_exits.py': leader_script},
        )
        body, rc = run_check_tick(
            self.db, self.config,
            now_fn=lambda: 1700000000.0,
            kill_grace=0.2,
            deliver_fn=lambda c, t: True,
        )
        self.assertEqual((rc, body['status']), (0, 'ok'))
        run = self.query_one('SELECT outcome, exit_code FROM check_runs')
        self.assertEqual(run['outcome'], 'failed_timeout')
        self.assertIsNone(run['exit_code'])
        # 子が SIGTERM 無視を設定済みであり、リーダーが SIGTERM で即終了したこと (既存テストと区別)
        self.assertTrue(child_ready_flag.exists())
        self.assertTrue(leader_exited_flag.exists())
        # TERM を無視した子プロセスも猶予後の SIGKILL で残っていないこと
        child_pid = int(child_pid_file.read_text(encoding='utf-8').strip())
        with self.assertRaises(ProcessLookupError):
            os.kill(child_pid, 0)

    def test_terminate_process_group_bounds_wait_after_sigkill(self):
        signals_sent = []
        wait_timeouts = []

        class UnkillableFakeProcess:
            pid = 999999
            returncode = None

            def poll(self):
                return None

            def wait(self, timeout=None):
                wait_timeouts.append(timeout)
                if timeout is None:
                    raise AssertionError('unbounded wait() called after SIGKILL')
                time.sleep(min(timeout, 0.05))
                raise subprocess.TimeoutExpired(cmd=['fake'], timeout=timeout)

        def fake_killpg(pgid, sig):
            signals_sent.append((pgid, sig))

        stderr_buf = io.StringIO()
        started = time.monotonic()
        with mock.patch('app.os.killpg', side_effect=fake_killpg), contextlib.redirect_stderr(stderr_buf):
            _terminate_process_group(UnkillableFakeProcess(), grace_sec=0.05, kill_wait_sec=0.05)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.5)
        self.assertIn((999999, signal.SIGTERM), signals_sent)
        self.assertIn((999999, signal.SIGKILL), signals_sent)
        self.assertGreaterEqual(len(wait_timeouts), 2)
        self.assertTrue(all(t is not None and t >= 0.0 for t in wait_timeouts))
        self.assertIn('warning:', stderr_buf.getvalue())
        self.assertIn('SIGKILL', stderr_buf.getvalue())

    # --- 7. JSONL 契約と検証違反 (failed_output) ---

    def test_jsonl_validation_and_failed_output_creates_zero_tasks(self):
        # 正常系 (空行・未知フィールド・空 body・key 省略)
        ok, evs = parse_check_jsonl(b'\n  \n{"title":"T","body":"","extra":123}\n')
        self.assertTrue(ok)
        self.assertEqual(evs, [('', 'T', '')])

        # 各種違反パターン
        invalid_cases = [
            b'a' * 65537,  # > 64KiB
            b'\xff\xfe',   # invalid UTF-8
            b'not json\n',
            b'[1, 2]\n',
            b'{"body":"b"}\n',  # missing title
            b'{"title":"t"}\n',  # missing body
            b'{"title":"","body":"b"}\n',  # empty title
            json.dumps({'title': 'x' * 201, 'body': 'b'}).encode(),  # > 200 code points
            b'{"key":null,"title":"t","body":"b"}\n',  # null key
            b'{"key":"","title":"t","body":"b"}\n',    # empty key
            b'{"key":"bad key!","title":"t","body":"b"}\n',  # invalid chars
            json.dumps({'key': 'a' * 201, 'title': 't', 'body': 'b'}).encode(),  # > 200 bytes
            b'{"key":"k1","title":"t1","body":"b"}\n{"key":"k1","title":"t2","body":"b"}\n',  # dup key
            b'{"title":"t1","body":"b"}\n{"title":"t2","body":"b"}\n',  # dup omitted key
        ]
        for raw in invalid_cases:
            valid, parsed = parse_check_jsonl(raw)
            self.assertFalse(valid, f'should be invalid: {raw[:60]!r}')
            self.assertIsNone(parsed)

        # 1 行目が正常でも 2 行目が不正なら 1 件も登録せず failed_output
        self.commit_config(
            '[[checks]]\nname = "badout"\ninterval = 60\ncommand = "scripts/bad.sh"\ntimeout = 10\nskill = "s1"\n',
            {'scripts/bad.sh': '#!/bin/sh\necho \'{"key":"k1","title":"ok","body":"b"}\'\necho \'{"key":"k1","title":"dup","body":"b"}\'\n'},
        )
        res = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000000'})
        self.assertEqual(res.returncode, 0)
        self.assertEqual(self.query_one('SELECT outcome FROM check_runs')['outcome'], 'failed_output')
        self.assertEqual(self.query_all('SELECT * FROM tasks'), [])
        self.assertEqual(self.query_all('SELECT * FROM check_tasks'), [])

        # exit != 0 は不正 stdout でも failed_exit が優先
        self.commit_config(
            '[[checks]]\nname = "badexit"\ninterval = 60\ncommand = "scripts/badexit.sh"\ntimeout = 10\nskill = "s1"\n',
            {'scripts/badexit.sh': '#!/bin/sh\necho "not-json"\nexit 2\n'},
        )
        self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000100'})
        self.assertEqual(self.query_one("SELECT outcome, exit_code FROM check_runs WHERE check_name='badexit'"), {'outcome': 'failed_exit', 'exit_code': 2})

    # --- 8. 重複抑制 (check, kind, dedup_key) と 20 件起票上限 (suppressed 非消費) ---

    def test_dedup_boundaries_and_20_cap_does_not_consume_suppressed(self):
        # 25 件出す script (k0..k24)
        emit_py = (
            '#!/usr/bin/env python3\nimport json\n'
            'for i in range(25):\n'
            '    print(json.dumps({"key": f"k{i:02d}", "title": f"ev {i}", "body": f"key: fake-{i}\\nbody {i}"}))\n'
        )
        self.commit_config(
            '[[checks]]\nname = "many"\ninterval = 60\ncommand = "scripts/many.py"\ntimeout = 10\nskill = "my-skill"\n',
            {'scripts/many.py': emit_py},
        )
        # 1 回目: k00..k19 の 20 件起票、k20..k24 の 5 件が events_deferred
        r1 = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000000'})
        self.assertEqual(r1.returncode, 0, r1.stderr)
        run1 = self.query_one('SELECT * FROM check_runs ORDER BY id DESC LIMIT 1')
        self.assertEqual(run1['outcome'], 'fired')
        self.assertEqual(run1['events_deferred'], 5)
        self.assertEqual(len(self.query_all('SELECT * FROM tasks')), 20)
        # skill は TOML の静的値、dedup_key は JSON の key (body の "key: fake" は無視)
        ct_keys = {r['dedup_key'] for r in self.query_all('SELECT dedup_key, skill FROM check_tasks')}
        self.assertEqual(ct_keys, {f'k{i:02d}' for i in range(20)})
        self.assertTrue(all(r['skill'] == 'my-skill' for r in self.query_all('SELECT skill FROM check_tasks')))

        # 2 回目: 先頭 20 件は suppressed (上限枠を消費しない!)、後ろ 5 件 (k20..k24) が新規起票!
        r2 = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000100'})
        self.assertEqual(r2.returncode, 0, r2.stderr)
        run2 = self.query_one('SELECT * FROM check_runs ORDER BY id DESC LIMIT 1')
        self.assertEqual(run2['outcome'], 'mixed')
        self.assertEqual(run2['events_deferred'], 0)
        self.assertEqual(len(self.query_all('SELECT * FROM tasks')), 25)
        ev2 = self.query_all('SELECT outcome, suppressed_by_task_id, task_id FROM check_run_events WHERE run_id=?', (run2['id'],))
        self.assertEqual(sum(1 for e in ev2 if e['outcome'] == 'suppressed' and e['suppressed_by_task_id']), 20)
        self.assertEqual(sum(1 for e in ev2 if e['outcome'] == 'fired' and e['task_id']), 5)

        # 3 回目: 全 25 件が suppressed
        r3 = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000200'})
        self.assertEqual(r3.returncode, 0)
        run3 = self.query_one('SELECT * FROM check_runs ORDER BY id DESC LIMIT 1')
        self.assertEqual(run3['outcome'], 'suppressed')
        self.assertEqual(run3['events_deferred'], 0)

        # k00 のタスクを done にすると k00 だけ再起票される
        k00_id = self.query_one("SELECT task_id FROM check_tasks WHERE dedup_key='k00'")['task_id']
        Store(self.db).reconcile_lane(k00_id, 'done')
        r4 = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000300'})
        self.assertEqual(r4.returncode, 0)
        run4 = self.query_one('SELECT * FROM check_runs ORDER BY id DESC LIMIT 1')
        self.assertEqual(run4['outcome'], 'mixed')
        self.assertEqual(len(self.query_all('SELECT * FROM tasks')), 26)

    def test_suppressed_events_after_cap_are_still_recorded(self):
        # k20 が既に未完了、script は n00..n19 (20 新規) + k20 (suppressed) + n20 (deferred) を出す
        emit_py = (
            '#!/usr/bin/env python3\nimport json, os\n'
            'if os.environ.get("PHASE") == "seed":\n'
            '    print(json.dumps({"key": "k20", "title": "seed", "body": ""}))\n'
            'else:\n'
            '    for i in range(20):\n'
            '        print(json.dumps({"key": f"n{i:02d}", "title": f"n {i}", "body": ""}))\n'
            '    print(json.dumps({"key": "k20", "title": "dup after cap", "body": ""}))\n'
            '    print(json.dumps({"key": "n20", "title": "deferred", "body": ""}))\n'
        )
        self.commit_config(
            '[[checks]]\nname = "cap"\ninterval = 60\ncommand = "scripts/cap.py"\ntimeout = 10\nskill = "s"\n',
            {'scripts/cap.py': emit_py},
        )
        self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000000', 'PHASE': 'seed'})
        self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000100', 'PHASE': 'full'})
        run = self.query_one('SELECT * FROM check_runs ORDER BY id DESC LIMIT 1')
        self.assertEqual(run['outcome'], 'mixed')
        self.assertEqual(run['events_deferred'], 1)
        evs = self.query_all('SELECT dedup_key, outcome, suppressed_by_task_id FROM check_run_events WHERE run_id=?', (run['id'],))
        self.assertEqual(len(evs), 21)
        supp = [e for e in evs if e['dedup_key'] == 'k20']
        self.assertEqual(len(supp), 1)
        self.assertEqual(supp[0]['outcome'], 'suppressed')
        self.assertIsNotNone(supp[0]['suppressed_by_task_id'])

    # --- 9. 故障 counter と故障タスクの状態遷移表・名前空間分離・TOML 変更リセット ---

    def test_fault_lifecycle_namespace_isolation_and_config_reset(self):
        self.commit_config(
            '[[checks]]\nname = "flaky"\ninterval = 60\ncommand = "scripts/flaky.sh"\ntimeout = 10\nskill = "ev-skill"\nfault_threshold = 3\n',
            {
                'scripts/flaky.sh': (
                    '#!/bin/sh\n'
                    'case "${MODE:-ok}" in\n'
                    '  fail) echo "boom" >&2; exit 1 ;;\n'
                    '  fire) echo \'{"title":"ev-no-key","body":"b"}\'; exit 0 ;;\n'
                    '  ok) exit 0 ;;\n'
                    'esac\n'
                ),
            },
        )
        t = 1700000000
        # まず key 省略 (dedup_key="") の通常タスクを起票しておく (故障タスクとの名前空間分離を検証)
        self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': str(t), 'MODE': 'fire'})
        ev_tasks = self.query_all("SELECT * FROM check_tasks WHERE kind='event' AND dedup_key=''")
        self.assertEqual(len(ev_tasks), 1)

        # 失敗 1 回目 -> fault_count = 1
        t += 100
        self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': str(t), 'MODE': 'fail'})
        self.assertEqual(self.query_one("SELECT fault_count FROM checks WHERE name='flaky'")['fault_count'], 1)

        # env / timeout のみの変更では fault_count はリセットされない!
        self.commit_config(
            '[[checks]]\nname = "flaky"\ninterval = 60\ncommand = "scripts/flaky.sh"\ntimeout = 20\nskill = "ev-skill"\nfault_threshold = 3\n'
            '[checks.env]\nFOO = "bar"\n',
        )
        t += 100
        self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': str(t), 'MODE': 'fail'})
        self.assertEqual(self.query_one("SELECT fault_count FROM checks WHERE name='flaky'")['fault_count'], 2)

        # 成功すると fault_count は 0 に戻る
        t += 100
        self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': str(t), 'MODE': 'ok'})
        self.assertEqual(self.query_one("SELECT fault_count FROM checks WHERE name='flaky'")['fault_count'], 0)

        # 連続 3 回失敗 -> 未完了の通常タスク (dedup_key="") があっても故障タスクが 1 件起票され fault_count=0!
        for _ in range(3):
            t += 100
            self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': str(t), 'MODE': 'fail'})
        faults = self.query_all("SELECT * FROM check_tasks WHERE kind='fault'")
        self.assertEqual(len(faults), 1)
        self.assertEqual((faults[0]['dedup_key'], faults[0]['skill']), ('', CHECK_FAULT_SKILL))
        self.assertEqual(self.query_one("SELECT fault_count FROM checks WHERE name='flaky'")['fault_count'], 0)

        # 未完了の故障タスクがある間の追加失敗 -> fault_count=0 のまま、二重起票なし
        t += 100
        self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': str(t), 'MODE': 'fail'})
        self.assertEqual(self.query_one("SELECT fault_count FROM checks WHERE name='flaky'")['fault_count'], 0)
        self.assertEqual(len(self.query_all("SELECT * FROM check_tasks WHERE kind='fault'")), 1)

        # 通常タスク (dedup_key="") を完了後、未完了の故障タスクが存在していても通常タスクを再起票できる (名前空間分離)
        Store(self.db).reconcile_lane(ev_tasks[0]['task_id'], 'done')
        t += 100
        self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': str(t), 'MODE': 'fire'})
        self.assertEqual(len(self.query_all("SELECT * FROM check_tasks WHERE kind='event'")), 2)

        # 故障タスクを完了させると、再度 3 回失敗で再起票される
        Store(self.db).reconcile_lane(faults[0]['task_id'], 'done')
        for _ in range(3):
            t += 100
            self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': str(t), 'MODE': 'fail'})
        self.assertEqual(len(self.query_all("SELECT * FROM check_tasks WHERE kind='fault'")), 2)

        # 故障タスクを完了して 2 回失敗させた後、command 変更で fault_count=0 にリセット
        latest_fault = self.query_all("SELECT task_id FROM check_tasks WHERE kind='fault' ORDER BY created_at")[-1]['task_id']
        Store(self.db).reconcile_lane(latest_fault, 'done')
        for _ in range(2):
            t += 100
            self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': str(t), 'MODE': 'fail'})
        self.assertEqual(self.query_one("SELECT fault_count FROM checks WHERE name='flaky'")['fault_count'], 2)

        _write_exec(self.repo / 'scripts/flaky_v2.sh', '#!/bin/sh\nexit 0\n')
        self.commit_config(
            '[[checks]]\nname = "flaky"\ninterval = 60\ncommand = "scripts/flaky_v2.sh"\ntimeout = 20\nskill = "ev-skill"\nfault_threshold = 3\n',
        )
        t += 10  # due でなくても mirror sync だけで command 変更によるリセットが効く
        self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': str(t)})
        self.assertEqual(self.query_one("SELECT fault_count FROM checks WHERE name='flaky'")['fault_count'], 0)

    # --- 10. wake (run-inbox 投入・回収・再試行・順序・20 秒上限・CB・開始時失敗スキップ) ---

    def test_wake_delivery_gc_retry_cb_order_and_budget(self):
        self.commit_config(
            '[[checks]]\nname = "w"\ninterval = 60\ncommand = "scripts/w.py"\ntimeout = 10\nskill = "wake-skill"\n',
            {
                'scripts/w.py': (
                    '#!/usr/bin/env python3\nimport json\n'
                    'for k in ("a", "b", "c"):\n'
                    '    print(json.dumps({"key": k, "title": f"UNTRUSTED TITLE {k}", "body": f"UNTRUSTED BODY {k}"}))\n'
                ),
            },
        )
        # 1 回目: 3 件起票、wake が全失敗 -> 2 件連続失敗で CB 発動し 3 件目は試行されない!
        r1 = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000000', 'FAKE_SEND_RC': '1'})
        self.assertEqual(r1.returncode, 0)
        b1 = json.loads(r1.stdout)
        self.assertEqual(b1['wake_failed'], 2)
        self.assertEqual(b1['unnotified_tasks'], 3)
        self.assertIn('warning:', r1.stderr)
        # wake のメッセージに非信頼テキスト (title / body) が混ざっていないこと
        sent_lines = [json.loads(line) for line in self.wake_log.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(sent_lines), 2)
        for entry in sent_lines:
            joined = ' '.join(entry['argv'])
            self.assertNotIn('UNTRUSTED', joined)
            self.assertIn('skill=wake-skill', joined)

        # 3 件目のタスクを claim 済み (pane 設定) にして回収されるか検証
        cts = self.query_all('SELECT task_id, dedup_key, last_wake_attempt_at FROM check_tasks ORDER BY dedup_key')
        unattempted = [c for c in cts if c['last_wake_attempt_at'] is None]
        self.assertEqual(len(unattempted), 1)
        claimed_id = unattempted[0]['task_id']
        Store(self.db).update_task(claimed_id, {'pane': 'w1:p2'})

        # 2 回目: 回収で claimed_id は notified_at=NULL のまま needs_wake=0 に落ち、残り 2 件が wake 成功
        self.wake_log.unlink()
        r2 = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000100', 'FAKE_SEND_RC': '0'})
        self.assertEqual(r2.returncode, 0)
        b2 = json.loads(r2.stdout)
        self.assertEqual(b2['wake_failed'], 0)
        self.assertEqual(b2['unnotified_tasks'], 0)

        claimed_row = self.query_one('SELECT needs_wake, notified_at FROM check_tasks WHERE task_id=?', (claimed_id,))
        self.assertEqual(claimed_row['needs_wake'], 0)
        self.assertIsNone(claimed_row['notified_at'])
        sent2 = [json.loads(line) for line in self.wake_log.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(sent2), 2)

    def test_wake_order_fault_priority_and_budget_truncation_and_no_end_retry(self):
        # DB を初期化
        self.commit_config(
            '[[checks]]\nname = "noop"\ninterval = 60\ncommand = "scripts/ok.sh"\ntimeout = 10\nskill = "s"\n',
            {'scripts/ok.sh': '#!/bin/sh\nexit 0\n'},
        )
        self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000000'})

        # 同時刻に event タスク (t_ev) と fault タスク (t_fl) を作成 -> fault が先に試行される
        ts = datetime.fromtimestamp(1700000010, timezone.utc).isoformat(timespec='microseconds')
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            for tid, kind, skill in (('t_aaa_ev', 'event', 's'), ('t_zzz_fl', 'fault', CHECK_FAULT_SKILL)):
                conn.execute(
                    "INSERT INTO tasks(id, title, lane, created_at, updated_at, kind, done_when) VALUES(?, ?, 'todo', ?, ?, 'monitor', 'w')",
                    (tid, tid, ts, ts),
                )
                conn.execute(
                    "INSERT INTO check_tasks(task_id, check_name, kind, dedup_key, skill, needs_wake, created_at) VALUES(?, 'noop', ?, '', ?, 1, ?)",
                    (tid, kind, skill, ts),
                )

        # pass_budget=7s, item_timeout=5s: 1 件目 (fault) に 3 秒かかって残り 4s < 5s -> 2 件目は打ち切り (truncated=True)
        clock = FakeClock(wall=1700000100.0, mono=100.0)
        seen = []

        def slow_deliver(cand, _timeout):
            seen.append(cand['task_id'])
            clock.advance(3.0)
            return True

        failed, truncated, attempted = run_wake_pass(
            self.db,
            now_iso_fn=lambda: datetime.fromtimestamp(clock.now(), timezone.utc).isoformat(timespec='microseconds'),
            mono_fn=clock.monotonic,
            pass_budget=7.0,
            item_timeout=5.0,
            deliver_fn=slow_deliver,
        )
        self.assertEqual(seen, ['t_zzz_fl'])
        self.assertEqual((failed, truncated, attempted), (set(), True, 1))

        # 開始時 wake で失敗したタスク (t_aaa_ev) は終了時 wake で再試行しない (distinct wake_failed == 1)
        attempts = []

        def fail_ev(cand, _timeout):
            attempts.append(cand['task_id'])
            return False

        err_buf = io.StringIO()
        with contextlib.redirect_stderr(err_buf):
            res, rc = run_check_tick(
                self.db, self.config,
                now_fn=lambda: 1700000200.0,
                mono_fn=clock.monotonic,
                deliver_fn=fail_ev,
            )
        self.assertEqual(rc, 0)
        self.assertEqual(attempts, ['t_aaa_ev'])  # 開始時の 1 回だけ (終了時 wake では再試行しない)
        self.assertEqual(res['wake_failed'], 1)
        self.assertIn('warning:', err_buf.getvalue())

    def test_wake_order_coalesce_moves_retried_task_behind_newer_unattempted(self):
        # DB を初期化
        self.commit_config(
            '[[checks]]\nname = "noop"\ninterval = 60\ncommand = "scripts/ok.sh"\ntimeout = 10\nskill = "s"\n',
            {'scripts/ok.sh': '#!/bin/sh\nexit 0\n'},
        )
        run_check_tick(self.db, self.config, now_fn=lambda: 1700000000.0, deliver_fn=lambda c, t: True)

        iso = lambda ts: datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec='microseconds')
        # created_at は t_retried_fault (T+5) < t_retried_ev (T+10) < t_fresh_newer (T+20) < t_newest (T+40) だが、
        # 再試行済み (last_wake_attempt_at = T+30) の 2 件は COALESCE(last_wake_attempt_at, created_at) に従い
        # より新しい未試行タスク t_fresh_newer (T+20) より後ろ、t_newest (T+40) より前に並ぶ。
        # created_at のみで並べる誤実装や NULLS FIRST/LAST の誤実装では一致しない入力。
        rows = [
            ('t_retried_fault', 'fault', CHECK_FAULT_SKILL, iso(1700000005), iso(1700000030)),
            ('t_retried_ev', 'event', 's', iso(1700000010), iso(1700000030)),
            ('t_fresh_newer', 'event', 's', iso(1700000020), None),
            ('t_newest', 'event', 's', iso(1700000040), None),
        ]
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            for tid, kind, skill, created_iso, attempt_iso in rows:
                conn.execute(
                    "INSERT INTO tasks(id, title, lane, created_at, updated_at, kind, done_when) VALUES(?, ?, 'todo', ?, ?, 'monitor', 'w')",
                    (tid, tid, created_iso, created_iso),
                )
                conn.execute(
                    "INSERT INTO check_tasks(task_id, check_name, kind, dedup_key, skill, needs_wake, last_wake_attempt_at, created_at) "
                    "VALUES(?, 'noop', ?, '', ?, 1, ?, ?)",
                    (tid, kind, skill, attempt_iso, created_iso),
                )

        clock = FakeClock(wall=1700000100.0, mono=100.0)
        order = []

        def record_deliver(cand, _timeout):
            order.append(cand['task_id'])
            return True

        failed, truncated, attempted = run_wake_pass(
            self.db,
            now_iso_fn=lambda: iso(clock.now()),
            mono_fn=clock.monotonic,
            deliver_fn=record_deliver,
        )
        self.assertEqual((failed, truncated, attempted), (set(), False, 4))
        self.assertEqual(order, ['t_fresh_newer', 't_retried_fault', 't_retried_ev', 't_newest'])

    # --- 11. 30 日 prune と CLI list / status 表示 ---

    def test_prune_30_days_and_cli_list_and_status_metrics(self):
        self.commit_config(
            '[[checks]]\nname = "active_chk"\ninterval = 300\ncommand = "scripts/ok.sh"\ntimeout = 10\nskill = "s1"\n'
            '[[checks]]\nname = "disabled_chk"\ninterval = 60\ncommand = "scripts/ok.sh"\ntimeout = 10\nskill = "s1"\nenabled = false\n'
            '[[checks]]\nname = "nohist_chk"\ninterval = 60\ncommand = "scripts/ok.sh"\ntimeout = 10\nskill = "s1"\n',
            {'scripts/ok.sh': '#!/bin/sh\nexit 0\n'},
        )
        now = 1700000000
        self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': str(now)})

        # 31 日前の古い run / event / tick と、12 時間前の deferred を手動挿入
        old_iso = datetime.fromtimestamp(now - 31 * 86400, timezone.utc).isoformat(timespec='microseconds')
        recent_iso = datetime.fromtimestamp(now - 3600, timezone.utc).isoformat(timespec='microseconds')
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            cur = conn.execute(
                "INSERT INTO check_ticks(started_at, finished_at, status, wake_failed, unnotified_tasks, checks_deferred) VALUES(?, ?, 'ok', 0, 0, 2)",
                (old_iso, old_iso),
            )
            old_tick_id = cur.lastrowid
            cur = conn.execute(
                "INSERT INTO check_runs(tick_id, check_name, started_at, finished_at, exit_code, outcome, events_deferred) VALUES(?, 'active_chk', ?, ?, 0, 'fired', 4)",
                (old_tick_id, old_iso, old_iso),
            )
            old_run_id = cur.lastrowid
            conn.execute(
                "INSERT INTO check_run_events(run_id, dedup_key, outcome, task_id) VALUES(?, 'oldk', 'fired', 't_old')",
                (old_run_id,),
            )
            # 直近 24 時間内の deferred 記録
            conn.execute(
                "INSERT INTO check_ticks(started_at, finished_at, status, wake_failed, unnotified_tasks, checks_deferred) VALUES(?, ?, 'ok', 0, 0, 3)",
                (recent_iso, recent_iso),
            )
            conn.execute(
                "INSERT INTO check_runs(tick_id, check_name, started_at, finished_at, exit_code, outcome, events_deferred) VALUES(1, 'active_chk', ?, ?, 0, 'fired', 7)",
                (recent_iso, recent_iso),
            )
            # nohist_chk の履歴を消して「履歴なしは最大遅延の対象外」を検証、disabled_chk に古い履歴を入れて対象外を検証
            conn.execute("UPDATE checks SET last_started=NULL, last_actual_started_at=NULL, last_outcome=NULL WHERE name='nohist_chk'")
            conn.execute(
                "UPDATE checks SET last_started=?, last_actual_started_at=?, last_outcome='ok_silent' WHERE name='disabled_chk'",
                (old_iso, old_iso),
            )

        # 次 tick (now + 100) で 31 日前の履歴が prune される
        res = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': str(now + 100)})
        self.assertEqual(res.returncode, 0)
        self.assertIsNone(self.query_one('SELECT id FROM check_runs WHERE id=?', (old_run_id,)))
        self.assertEqual(self.query_all('SELECT * FROM check_run_events WHERE run_id=?', (old_run_id,)), [])
        self.assertIsNone(self.query_one('SELECT id FROM check_ticks WHERE id=?', (old_tick_id,)))

        # prune tick 後に nohist_chk の履歴を消して「履歴なしは最大遅延の対象外」を検証
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE checks SET last_started=NULL, last_actual_started_at=NULL, last_outcome=NULL WHERE name='nohist_chk'")

        # check list / check status の確認 (now + 500 時点で active_chk の遅延は (500 - 300) = 200s)
        lst = json.loads(self.cli('check', 'list', '--json').stdout)
        self.assertEqual({c['name'] for c in lst['checks']}, {'active_chk', 'disabled_chk', 'nohist_chk'})
        lst_text = self.cli('check', 'list').stdout
        self.assertIn('active_chk', lst_text)

        st = json.loads(self.cli('check', 'status', '--json', env_extra={'CUE_CHECK_NOW': str(now + 500)}).stdout)
        self.assertIsNotNone(st['last_ok_tick_at'])
        self.assertEqual(st['checks_deferred_24h'], {'total': 3, 'ticks': 1})
        self.assertEqual(st['events_deferred_24h'], 7)
        self.assertEqual(st['max_overdue_check'], 'active_chk')
        self.assertAlmostEqual(st['max_overdue_sec'], 200.0, delta=2.0)

        st_text = self.cli('check', 'status', env_extra={'CUE_CHECK_NOW': str(now + 500)}).stdout
        self.assertIn('last_ok_tick_at:', st_text)
        self.assertIn('active_chk', st_text)

    def test_mirror_sync_disables_removed_and_disabled_checks_preserving_history(self):
        self.commit_config(
            '[[checks]]\nname = "keep"\ninterval = 60\ncommand = "scripts/fail.sh"\ntimeout = 10\nskill = "s"\nfault_threshold = 5\n'
            '[[checks]]\nname = "drop"\ninterval = 60\ncommand = "scripts/fail.sh"\ntimeout = 10\nskill = "s"\nfault_threshold = 5\n',
            {'scripts/fail.sh': '#!/bin/sh\nexit 1\n'},
        )
        self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000000'})
        self.assertEqual(self.query_one("SELECT fault_count FROM checks WHERE name='keep'")['fault_count'], 1)
        self.assertEqual(self.query_one("SELECT fault_count FROM checks WHERE name='drop'")['fault_count'], 1)

        # keep は無効化、drop は TOML から削除 -> 両方 enabled=0, fault_count=0、履歴は残る
        self.commit_config(
            '[[checks]]\nname = "keep"\ninterval = 60\ncommand = "scripts/fail.sh"\ntimeout = 10\nskill = "s"\nfault_threshold = 5\nenabled = false\n',
        )
        self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000100'})
        for name in ('keep', 'drop'):
            row = self.query_one('SELECT enabled, fault_count FROM checks WHERE name=?', (name,))
            self.assertEqual((row['enabled'], row['fault_count']), (0, 0))
        self.assertEqual(len(self.query_all('SELECT * FROM check_runs')), 2)

    def test_command_cwd_env_and_4k_multibyte_excerpt_limit(self):
        self.commit_config(
            '[[checks]]\nname = "envcwd"\ninterval = 60\ncommand = "scripts/envcwd.py"\ntimeout = 10\nskill = "s"\n'
            '[checks.env]\nCUSTOM_VAR = "hello-env"\n',
            {
                'scripts/envcwd.py': (
                    '#!/usr/bin/env python3\nimport json, os, sys\n'
                    'assert os.path.realpath(os.getcwd()) == os.path.realpath(os.path.dirname(os.path.dirname(__file__)))\n'
                    'assert os.environ.get("CUSTOM_VAR") == "hello-env"\n'
                    'sys.stderr.write("あ" * 3000)\n'
                    'print(json.dumps({"key": "k1", "title": "ok", "body": "い" * 2000}))\n'
                ),
            },
        )
        res = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000000'})
        self.assertEqual(res.returncode, 0, res.stderr)
        run = self.query_one('SELECT outcome, stdout_excerpt, stderr_excerpt FROM check_runs')
        self.assertEqual(run['outcome'], 'fired')
        self.assertLessEqual(len(run['stdout_excerpt'].encode('utf-8')), 4096)
        self.assertLessEqual(len(run['stderr_excerpt'].encode('utf-8')), 4096)
        # tasks.request は TASK_LIMITS['request'] (1000 文字) 以内に収まり reconcile-lane 等と互換
        task = self.query_one('SELECT request FROM tasks')
        self.assertLessEqual(len(task['request']), 1000)

    def test_git_timeout_triggers_config_error(self):
        self.commit_config(
            '[[checks]]\nname = "c1"\ninterval = 60\ncommand = "scripts/ok.sh"\ntimeout = 10\nskill = "s"\n',
            {'scripts/ok.sh': '#!/bin/sh\nexit 0\n'},
        )
        # mono_fn が呼ばれるたびに 3 秒進むようにして git 合計 5 秒上限を超過させる
        mono = {'t': 0.0}

        def fast_advancing_mono():
            mono['t'] += 3.0
            return mono['t']

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            res, rc = run_check_tick(
                self.db, self.config,
                now_fn=lambda: 1700000000.0,
                mono_fn=fast_advancing_mono,
                deliver_fn=lambda c, t: True,
            )
        self.assertEqual(rc, 1)
        self.assertEqual(res['status'], 'config_error')
        self.assertIn('5 秒', res['error'])


if __name__ == '__main__':
    unittest.main()
