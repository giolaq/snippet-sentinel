import os
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
    assert owner_namespace() == ''
    assert owner_container() == ''
    cleanup_stale()
    assert run(Snippet('x', 'a.md', 1, 'python', 'print(1)'))['status'] == 'pass'
    assert any('snippet-sentinel.namespace=' in c for c in commands)


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
