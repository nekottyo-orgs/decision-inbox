#!/usr/bin/env python3
"""Update an existing macOS Cue installation from an explicit GitHub release."""

import argparse
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time

REPO = 'nekottyo/decision-inbox'
ARCHIVE = 'decision-inbox.tar.gz'
PLUGIN = 'nekottyo.cue'
REQUIRED = {
    'app.py', 'cue_tui.py', 'assets/demo-flow.svg',
    'frontend/dist/index.html', 'frontend/dist/assets/app.js', 'frontend/dist/assets/app.css',
    'herdr-plugin/cue_plugin.py', 'herdr-plugin/herdr-plugin.toml',
}


class UpdateError(Exception):
    pass


def run(error, *args, cwd=None):
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise UpdateError(f'{error} (exit {result.returncode})')
    return result.stdout


def commit_for(tag):
    commit = run('Cannot resolve release tag', 'gh', 'api', f'repos/{REPO}/commits/{tag}',
                 '--jq', '.sha').strip()
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise UpdateError('Invalid release commit from GitHub')
    return commit


def sha256(path):
    with path.open('rb') as source:
        digest = hashlib.sha256()
        for block in iter(lambda: source.read(65536), b''):
            digest.update(block)
        return digest.hexdigest()


def download(tag, directory):
    release = json.loads(run('Cannot read release', 'gh', 'release', 'view', tag, '--repo', REPO,
                             '--json', 'tagName,isDraft'))
    if release['tagName'] != tag or release['isDraft']:
        raise UpdateError('Expected the specified published GitHub release')
    commit = commit_for(tag)
    run('Cannot download release assets', 'gh', 'release', 'download', tag, '--repo', REPO,
        '--pattern', ARCHIVE, '--pattern', f'{ARCHIVE}.sha256', '--dir', directory)
    checksum = (directory / f'{ARCHIVE}.sha256').read_text(encoding='ascii')
    match = re.fullmatch(r'([0-9a-fA-F]{64}) [ *]decision-inbox\.tar\.gz\r?\n?', checksum)
    archive = directory / ARCHIVE
    if not match or match[1].lower() != sha256(archive):
        raise UpdateError('Release archive SHA-256 mismatch')
    if commit_for(tag) != commit:
        raise UpdateError('The tag moved while downloading the release')
    return commit, archive


def unpack(archive, destination):
    destination.mkdir()
    paths = set()
    with tarfile.open(archive, 'r:gz') as package:
        for entry in package:
            path = PurePosixPath(entry.name)
            if (not entry.isfile() or path.is_absolute() or not path.parts
                    or '..' in path.parts or path.as_posix() != entry.name or path in paths):
                raise UpdateError(f'Unexpected archive entry: {entry.name!r}')
            paths.add(path)
            output = destination / path
            output.parent.mkdir(parents=True, exist_ok=True)
            with package.extractfile(entry) as source, output.open('xb') as target:
                shutil.copyfileobj(source, target)
    if not REQUIRED.issubset({str(path) for path in paths}):
        raise UpdateError('Release archive is missing required files')
    (destination / 'app.py').chmod(0o755)
    return paths


def cli_list(release, db):
    result = json.loads(run('CLI could not open the DB copy', sys.executable, release / 'app.py',
                            '--db', db, 'list', '--include-withdrawn'))
    if not isinstance(result.get('decisions'), list):
        raise UpdateError('Invalid CLI decisions response')
    return result


def backup_and_check(previous, candidate, db, root, temporary):
    backups = root / 'backups'
    backups.mkdir(mode=0o700, exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix='before-release-', dir=backups)) / 'decisions.sqlite3'
    run('DB backup failed', sys.executable, previous / 'app.py', '--db', db,
        'backup', '--output', backup)
    print(f'DB backup: {backup}', flush=True)
    copy = temporary / 'decisions-copy.sqlite3'
    shutil.copyfile(backup, copy)
    original_list = cli_list(previous, copy)
    cli_list(candidate, copy)
    run('Candidate TUI could not load', sys.executable, '-B', '-c', 'import cue_tui', cwd=candidate)
    if cli_list(previous, copy) != original_list:
        raise UpdateError('The previous release read different decisions after opening with the candidate')


def verify_plugin(release):
    result = json.loads(run('Cannot check Herdr plugin', 'herdr', 'plugin', 'list',
                            '--plugin', PLUGIN, '--json'))
    plugins = result['result']['plugins']
    if len(plugins) != 1 or plugins[0]['plugin_id'] != PLUGIN or not plugins[0]['enabled']:
        raise UpdateError('The Cue plugin is not active in this Herdr session')
    if Path(plugins[0]['plugin_root']).resolve() != release / 'herdr-plugin':
        raise UpdateError('Herdr is linked to a different Cue release')


def restart(service):
    run('Cannot restart Cue Web', 'launchctl', 'kickstart', '-k', f'gui/{os.getuid()}/{service}')


def web_check(release, port):
    files = {'/': 'index.html', '/assets/app.js': 'assets/app.js', '/assets/app.css': 'assets/app.css'}
    expected = {url: (release / 'frontend/dist' / path).read_bytes()
                for url, path in files.items()}
    until = time.monotonic() + 15
    while True:
        try:
            actual = {}
            for url in ('/api/decisions', *files):
                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=1)
                try:
                    connection.request('GET', url)
                    response = connection.getresponse()
                    if response.status != 200:
                        raise UpdateError(f'Cue Web returned HTTP {response.status}')
                    actual[url] = response.read()
                finally:
                    connection.close()
            if (isinstance(json.loads(actual['/api/decisions']).get('decisions'), list)
                    and all(actual[url] == content for url, content in expected.items())):
                return
        except (OSError, http.client.HTTPException, ValueError, AttributeError, UpdateError):
            pass
        if time.monotonic() >= until:
            raise UpdateError('Cue Web did not serve the expected page, API and assets')
        time.sleep(0.25)


def relink(release):
    run('Cannot link Cue plugin in Herdr', 'herdr', 'plugin', 'link', release / 'herdr-plugin')
    verify_plugin(release)


def switch(current, target, previous, temporary):
    if os.readlink(current) != previous:
        raise UpdateError('current changed outside this update')
    replacement = temporary / 'current-next'
    replacement.symlink_to(target)
    os.replace(replacement, current)


def activate(args, cli, current, previous_target, old, candidate, temporary):
    verify_plugin(old)
    web_check(old, args.port)
    print(f'Previous release: {old}\nCandidate release: {candidate}', flush=True)
    candidate_target = str(candidate)
    try:
        switch(current, candidate_target, previous_target, temporary)
        run('CLI could not start', cli, '--help')
        relink(candidate)
        restart(args.service)
        web_check(candidate, args.port)
    except (Exception, KeyboardInterrupt):
        if os.readlink(current) != candidate_target:
            raise
        try:
            switch(current, previous_target, candidate_target, temporary)
        except (OSError, UpdateError) as error:
            print(f'Could not restore current: {error}', file=sys.stderr)
            raise
        print('Restored previous current link', file=sys.stderr)
        try:
            relink(old)
        finally:
            restart(args.service)
            web_check(old, args.port)
        raise


def update(args):
    root = args.root.expanduser().resolve(strict=True)
    current = root / 'current'
    releases = root / 'releases'
    cli = args.cli.expanduser().absolute()
    if not releases.is_dir() or releases.is_symlink():
        raise UpdateError('Expected a releases directory inside the Cue install root')
    db = args.db.expanduser().resolve(strict=True)
    if not db.is_file() or db.is_relative_to(releases):
        raise UpdateError('Expected an existing DB outside the releases directory')
    cli_target = Path(os.path.abspath(cli.parent / os.readlink(cli)))
    if cli_target.parts[-2:] != ('current', 'app.py') or cli_target.parent.parent.resolve() != root:
        raise UpdateError('The existing CLI symlink must point to current/app.py')

    with (root / '.update-release.lock').open('a') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UpdateError('Another release update is active') from None
        previous_target = os.readlink(current)
        old = current.resolve(strict=True)
        if old.parent != releases or not old.is_dir():
            raise UpdateError('current must point to a direct child of releases')
        with tempfile.TemporaryDirectory(prefix='.cue-update-', dir=root) as name:
            temporary = Path(name)
            commit, archive = download(args.tag, temporary)
            staged = temporary / 'release'
            files = unpack(archive, staged)
            candidate = releases / commit
            if candidate.exists() or candidate.is_symlink():
                if (candidate.is_symlink() or not candidate.is_dir()
                        or not os.access(candidate / 'app.py', os.X_OK)
                        or any((candidate / path).resolve() != candidate / path
                               or not (candidate / path).is_file()
                               or sha256(candidate / path) != sha256(staged / path) for path in files)):
                    raise UpdateError('An existing release differs from the verified archive')
            if candidate == old:
                run('CLI could not start', cli, '--help')
                verify_plugin(candidate)
                web_check(candidate, args.port)
                print(f'Already active: {args.tag} ({commit})')
                return
            backup_and_check(old, staged, db, root, temporary)
            if not candidate.exists():
                staged.rename(candidate)
            activate(args, cli, current, previous_target, old, candidate, temporary)
            print(f'Updated to {args.tag} ({commit}). Reopen the Herdr popup to verify TUI.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('tag', help='explicit published tag, such as v0.0.1')
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--service', required=True, help='existing launchd service label')
    parser.add_argument('--port', type=int, required=True, help='existing loopback Web port')
    parser.add_argument('--root', type=Path, default=Path('~/.local/share/cue'))
    parser.add_argument('--cli', type=Path, default=Path('~/.local/bin/decision-inbox'))
    args = parser.parse_args()
    if not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.+-]+)?', args.tag):
        parser.error('specify an explicit Cue version tag')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', args.service) or not 1 <= args.port <= 65535:
        parser.error('invalid launchd service label or Web port')

    def interrupted(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    try:
        update(args)
    except (UpdateError, OSError, ValueError, TypeError, KeyError, AttributeError,
            tarfile.TarError, subprocess.TimeoutExpired, KeyboardInterrupt) as error:
        print(f'Cue update failed: {error or "interrupted"}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
