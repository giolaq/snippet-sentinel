import json
import os
import shutil
import socket
import sys
import threading
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from snippet_sentinel.extractor import Snippet
from snippet_sentinel.network_diagnostics import blocked_hosts, dead_host
from snippet_sentinel.network_guard import MARKER
from snippet_sentinel.runner import command, run


def evidence(*hosts):
    return '\n'.join(MARKER + ' ' + json.dumps(host) for host in hosts)


def test_blocked_hosts_validates_normalizes_and_bounds():
    output = evidence({}, [], None, 'bad\nhost', 'bad/host', 'a' * 254,
                      '127.0.0.1', '192.0.2.1', '2001:db8::1', 'Example.COM.', 'example.com',
                      'BÜCHER.example.', *[f'h{i}.example' for i in range(20)])
    output = MARKER + ' invalid json\n' + output
    assert blocked_hosts(output) == ['example.com', 'xn--bcher-kva.example',
                                     *[f'h{i}.example' for i in range(8)]]


@pytest.mark.parametrize('error,expected', [
    (None, None),
    (socket.gaierror(socket.EAI_NONAME, 'Name or service not known'), 'dead.example'),
    (socket.gaierror(getattr(socket, 'EAI_NODATA', socket.EAI_NONAME),
                     'No address associated with hostname'), 'dead.example'),
    (socket.gaierror(socket.EAI_AGAIN, 'Temporary failure in name resolution'), None),
    (socket.gaierror(socket.EAI_FAIL, 'Resolver unavailable'), None),
    (OSError('resolver unavailable'), None),
    (TimeoutError('DNS timed out'), None),
])
def test_dns_outcomes(monkeypatch, error, expected):
    resolver = Mock(side_effect=error, return_value=[])
    monkeypatch.setattr('snippet_sentinel.network_diagnostics.socket.getaddrinfo', resolver)
    monkeypatch.setattr(socket, 'create_connection', Mock(side_effect=AssertionError('no connects')))
    assert dead_host(evidence('dead.example')) == expected
    resolver.assert_called_once_with('dead.example', None)
    socket.create_connection.assert_not_called()


def test_any_definitive_failure_wins(monkeypatch):
    def resolve(host, port):
        if host == 'dead.example':
            raise socket.gaierror(socket.EAI_NONAME, 'not found')
        return []

    monkeypatch.setattr('snippet_sentinel.network_diagnostics.socket.getaddrinfo', resolve)
    assert dead_host(evidence('live.example', 'dead.example')) == 'dead.example'


@pytest.mark.parametrize('output', ['', MARKER, evidence('192.0.2.1', '2001:db8::1'),
                                  evidence(None, 'bad/path')])
def test_missing_hosts_and_literal_ips_do_not_resolve(monkeypatch, output):
    resolver = Mock()
    monkeypatch.setattr('snippet_sentinel.network_diagnostics.socket.getaddrinfo', resolver)
    assert dead_host(output) is None
    resolver.assert_not_called()


def test_dns_threads_have_bounded_wait(monkeypatch):
    release = threading.Event()
    calls = []

    def resolve(host, port):
        calls.append((host, threading.current_thread().daemon))
        release.wait(5)

    monkeypatch.setattr('snippet_sentinel.network_diagnostics.socket.getaddrinfo', resolve)
    started = time.monotonic()
    try:
        assert dead_host(evidence(*[f'h{i}.example' for i in range(20)]), timeout=.05) is None
        assert time.monotonic() - started < 1
        assert len(calls) == 10
        assert all(daemon for host, daemon in calls)
    finally:
        release.set()


def test_unavailable_dns_thread_is_inconclusive(monkeypatch):
    monkeypatch.setattr('snippet_sentinel.network_diagnostics.threading.Thread.start',
                        Mock(side_effect=RuntimeError('cannot start thread')))
    assert dead_host(evidence('dead.example')) is None


@pytest.mark.parametrize('lang', ['python', 'javascript'])
def test_guards_record_first_ten_unique_hosts(tmp_path, lang):
    hosts = ['one.example', 'one.example', *[f'h{i}.example' for i in range(20)]]
    if lang == 'python':
        code = ('import socket\nfrom snippet_sentinel.network_guard import install\ninstall()\n'
                f'for host in {hosts!r}:\n'
                ' try: socket.getaddrinfo(host, 443)\n except OSError: pass\n')
        args = [sys.executable, '-c', code]
    else:
        node = shutil.which('node')
        if not node:
            pytest.skip('Host Node is not installed')
        guard = Path(__file__).parents[1] / 'snippet_sentinel/network_guard.cjs'
        code = f"const dns = require('node:dns'); for (const h of {json.dumps(hosts)}) dns.lookup(h,()=>{{}});"
        args = [node, '--require', str(guard), '-e', code]
    output, rc, expired = command(args, tmp_path, 5)
    assert rc == 0 and not expired, output
    assert blocked_hosts(output) == ['one.example', *[f'h{i}.example' for i in range(9)]]
    assert output.count(MARKER) <= 20


def test_host_evidence_survives_output_truncation(tmp_path):
    hosts = [f'h{i}.example' for i in range(10)]
    code = f'print({evidence(*hosts)!r}, flush=True); print("x" * 20000)'
    output, rc, expired = command([sys.executable, '-c', code], tmp_path, 5)
    assert rc == 0 and not expired
    assert blocked_hosts(output) == hosts


@pytest.mark.parametrize('backend,allowed,terminal,expired,host,expected,checked', [
    ('docker', False, 'ConnectError: blocked', False, 'dead.example', 'runtime_error', True),
    ('docker', False, 'ConnectError: blocked', False, None, 'needs_network', True),
    ('docker', False, 'ConnectError: blocked', True, 'dead.example', 'runtime_error', True),
    ('docker', False, 'ConnectError: blocked', True, None, 'needs_network', True),
    ('docker', False, 'ValueError: broken', False, 'dead.example', 'runtime_error', False),
    ('docker', False, 'SENTINEL_OUTPUT_LIMIT: exceeded', False, 'dead.example', 'output_limit', False),
    ('docker', True, 'ConnectError: blocked', False, 'dead.example', 'runtime_error', False),
    ('local', False, 'ConnectError: blocked', False, 'dead.example', 'runtime_error', False),
])
def test_runner_dns_verdicts(monkeypatch, backend, allowed, terminal, expired, host, expected, checked):
    output = evidence('dead.example') + '\n' + terminal

    def fake_command(args, *unused, **kwargs):
        return (output, 1, expired) if 'worker.py' in args else ('', 0, False)

    check = Mock(return_value=host)
    monkeypatch.setattr('snippet_sentinel.runner.command', fake_command)
    monkeypatch.setattr('snippet_sentinel.runner.subprocess.run', lambda *args, **kwargs: None)
    monkeypatch.setattr('snippet_sentinel.runner.dead_host', check)
    result = run(Snippet('dns', 'a.md', 1, 'python', 'print(1)'), runner=backend, allow_network=allowed)
    assert result['status'] == expected, result
    assert check.called == checked
    assert ('host dead.example does not resolve (dead URL in docs)' in result['stderr']) == (
        checked and host is not None)


@pytest.mark.docker
@pytest.mark.skipif(not os.environ.get('SENTINEL_DOCKER_TESTS'), reason='Set SENTINEL_DOCKER_TESTS=1')
@pytest.mark.parametrize('lang,code', [
    ('python', "import socket\nsocket.create_connection(('dead-docs.invalid', 443))"),
    ('javascript', "await fetch('https://dead-docs.invalid')"),
])
def test_docker_dead_host_evidence(monkeypatch, lang, code):
    def resolve(host, port):
        assert host == 'dead-docs.invalid'
        raise socket.gaierror(socket.EAI_NONAME, 'not found')

    monkeypatch.setattr('snippet_sentinel.network_diagnostics.socket.getaddrinfo', resolve)
    result = run(Snippet('dns', 'a.md', 1, lang, code), timeout=30)
    assert result['status'] == 'runtime_error', result
    assert 'host dead-docs.invalid does not resolve (dead URL in docs)' in result['stderr']
