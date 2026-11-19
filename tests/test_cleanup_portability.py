import os
import json
import subprocess
from pathlib import Path

import pytest

from snippet_sentinel.extractor import Snippet
from snippet_sentinel.runner import cleanup_stale, owner_container, owner_identity, owner_namespace, run


def test_missing_proc_does_not_prevent_docker_scan(monkeypatch):
    read_text = Path.read_text

    def without_proc(path, *args, **kwargs):
        if str(path).startswith('/proc/'):
            raise FileNotFoundError(str(path))
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', without_proc)
    monkeypatch.setattr('snippet_sentinel.runner.os.readlink', lambda path: (_ for _ in ()).throw(FileNotFoundError(path)))
    commands = []

    def fake_command(args, *a, **kw):
        commands.append(args)
        return '', 0, False

    monkeypatch.setattr('snippet_sentinel.runner.command', fake_command)
    monkeypatch.setattr('snippet_sentinel.runner.subprocess.run', lambda args, **kw: subprocess.CompletedProcess(args, 0, '', ''))
    assert owner_identity().startswith(f'{os.getpid()}:unknown-')
    assert owner_namespace().startswith('host:')
    assert owner_container() == ''
    cleanup_stale()
    assert run(Snippet('x', 'a.md', 1, 'python', 'print(1)'))['status'] == 'pass'
    assert any(any(arg.startswith('snippet-sentinel.namespace=host:') for arg in c) for c in commands)


@pytest.mark.parametrize('error', [FileNotFoundError('docker'), subprocess.TimeoutExpired('docker', 10), ValueError('bad metadata')])
def test_cleanup_is_best_effort(monkeypatch, error):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr('snippet_sentinel.runner.subprocess.run', fail)
    cleanup_stale()


@pytest.mark.docker
@pytest.mark.skipif(not os.environ.get('SENTINEL_DOCKER_TESTS'), reason='Set SENTINEL_DOCKER_TESTS=1')
def test_installed_console_script_is_on_path():
    result = run(Snippet('black', 'a.md', 1, 'python', "import subprocess\nimport black\nsubprocess.run(['black','--version'],check=True)"), timeout=45)
    assert result['status'] == 'pass', result
    assert 'black,' in result['stderr']


@pytest.mark.parametrize('state,removed', [('permission', False), ('io', False), ('malformed', False), ('gone', True), ('mismatch', True), ('live', False)])
def test_only_definitely_dead_owners_are_reaped(monkeypatch, state, removed):
    calls = []
    labels = {'snippet-sentinel.namespace': 'namespace', 'snippet-sentinel.owner': '123:old'}

    def fake_run(args, **kwargs):
        calls.append(args)
        output = 'sandbox\n' if args[1] == 'ps' else json.dumps(labels) if args[1] == 'inspect' else ''
        return subprocess.CompletedProcess(args, 0, output, '')

    def fake_read(path, *args, **kwargs):
        if state == 'permission':
            raise PermissionError(str(path))
        if state == 'io':
            raise OSError('unreadable')
        if state == 'gone':
            raise FileNotFoundError(str(path))
        if state == 'malformed':
            return 'unknown'
        return '123 (owner) ' + ' '.join(['S'] * 19 + ['old' if state == 'live' else 'new'])

    monkeypatch.setattr('snippet_sentinel.runner.subprocess.run', fake_run)
    monkeypatch.setattr('snippet_sentinel.runner.owner_namespace', lambda: 'namespace')
    monkeypatch.setattr(Path, 'read_text', fake_read)
    cleanup_stale()
    assert (['docker', 'rm', '-f', 'sandbox'] in calls) == removed


@pytest.mark.parametrize('state,removed', [('gone', True), ('live', False), ('denied', False), ('io', False),
                                          ('reused', True), ('unknown', False), ('foreign', False)])
def test_portable_same_host_cleanup(monkeypatch, state, removed):
    calls = []
    labels = {'snippet-sentinel.namespace': 'host:other' if state == 'foreign' else 'host:mac',
              'snippet-sentinel.owner': '123:ps-Thu Nov 19 02:00:00 2026'}

    def fake_run(args, **kwargs):
        calls.append(args)
        output = 'sandbox\n' if args[1] == 'ps' else json.dumps(labels) if args[1] == 'inspect' else ''
        return subprocess.CompletedProcess(args, 0, output, '')

    def fake_kill(pid, sig):
        assert (pid, sig) == (123, 0)
        assert state != 'foreign'
        if state == 'gone':
            raise ProcessLookupError()
        if state == 'denied':
            raise PermissionError()
        if state == 'io':
            raise OSError('unknown')

    monkeypatch.setattr('snippet_sentinel.runner.owner_namespace', lambda: 'host:mac')
    monkeypatch.setattr('snippet_sentinel.runner.portable_start', lambda pid: '' if state == 'unknown' else 'new' if state == 'reused' else 'Thu Nov 19 02:00:00 2026')
    monkeypatch.setattr('snippet_sentinel.runner.os.kill', fake_kill)
    monkeypatch.setattr('snippet_sentinel.runner.subprocess.run', fake_run)
    cleanup_stale()
    assert (['docker', 'rm', '-f', 'sandbox'] in calls) == removed


def test_portable_live_owner_survives_timezone_and_locale_change(monkeypatch):
    calls, environments, labels = [], [], {}

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[0] == 'ps':
            env = dict(kwargs.get('env', os.environ))
            environments.append(env)
            output = f"Thu Nov 19 02:00:00 2026 {env.get('TZ')}/{env.get('LC_ALL')}"
        else:
            output = 'sandbox\n' if args[1] == 'ps' else json.dumps(labels) if args[1] == 'inspect' else ''
        return subprocess.CompletedProcess(args, 0, output, '')

    def missing_proc(path, *args, **kwargs):
        raise FileNotFoundError(str(path))

    monkeypatch.setattr(Path, 'read_text', missing_proc)
    monkeypatch.setattr('snippet_sentinel.runner.subprocess.run', fake_run)
    monkeypatch.setattr('snippet_sentinel.runner.owner_namespace', lambda: 'host:mac')
    monkeypatch.setattr('snippet_sentinel.runner.os.kill', lambda pid, sig: None)
    monkeypatch.setenv('TZ', 'America/Los_Angeles')
    monkeypatch.setenv('LC_ALL', 'fr_FR.UTF-8')
    labels.update({'snippet-sentinel.namespace': 'host:mac', 'snippet-sentinel.owner': owner_identity()})
    monkeypatch.setenv('TZ', 'Asia/Tokyo')
    monkeypatch.setenv('LC_ALL', 'ja_JP.UTF-8')
    cleanup_stale()
    assert len(environments) == 2
    assert all(env['TZ'] == 'UTC' and env['LC_ALL'] == 'C' for env in environments)
    assert ['docker', 'rm', '-f', 'sandbox'] not in calls
