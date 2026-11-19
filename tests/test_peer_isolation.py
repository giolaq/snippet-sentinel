import json
import os
import sys

import pytest

from snippet_sentinel.extractor import Snippet
from snippet_sentinel.runner import command, run


@pytest.mark.parametrize('peers,expected,installs', [
    (['ws', None], 'pass', ['ws']),
    (['ws', 'ws'], 'missing_dependency', ['ws']),
    (['ws', 'undici', 'bufferutil', 'utf-8-validate'], 'missing_dependency', ['ws', 'undici', 'bufferutil']),
    ([''], 'missing_dependency', []),
    (['--bad'], 'missing_dependency', []),
])
def test_host_peer_retry_is_bounded_and_isolated(monkeypatch, peers, expected, installs):
    calls, limits, budgets = [], [], []
    pending = iter(peers)
    connected = True

    def fake_command(args, cwd, timeout, env=None, stdin=None, budget=None):
        nonlocal connected
        calls.append(args)
        limits.append(timeout)
        budgets.append(budget)
        if args[:3] == ['docker', 'network', 'disconnect']:
            connected = False
        elif args[:3] == ['docker', 'network', 'connect']:
            connected = True
        if 'npm' in args:
            assert connected
        if 'worker.cjs' in args:
            assert not connected
            assert 'SENTINEL_NETWORK_DISABLED=1' in args
            peer = next(pending)
            if peer is not None:
                return f"Error: Cannot find module '{peer}'\nRequire stack:\n- /work/node_modules/sdk/index.js\nSENTINEL_NEED_PEER={peer}", 86, False
            assert json.loads(args[-1])[0] == installs
            return 'SENTINEL_VERSIONS=' + json.dumps({p: '1.0.0' for p in installs}), 0, False
        return '', 0, False

    monkeypatch.setattr('snippet_sentinel.runner.command', fake_command)
    monkeypatch.setattr('snippet_sentinel.runner.subprocess.run', lambda *a, **kw: None)
    result = run(Snippet('peer', 'a.md', 1, 'javascript', 'console.log(1)'))
    assert result['status'] == expected, result
    assert [args[-1] for args in calls if 'npm' in args] == installs
    assert all(budget is budgets[0] for budget in budgets)
    assert limits == sorted(limits, reverse=True)
    if expected == 'pass':
        assert result['versions'] == {p: '1.0.0' for p in installs}


@pytest.mark.parametrize('failure,expected', [('install', 'missing_dependency'), ('timeout', 'timeout'),
                                            ('output', 'output_limit'), ('alive', 'runtime_error')])
def test_failed_peer_setup_never_reexecutes_snippet(monkeypatch, failure, expected):
    calls = []

    def fake_command(args, *unused):
        calls.append(args)
        if 'worker.cjs' in args:
            return "Error: Cannot find module 'ws'\nSENTINEL_NEED_PEER=ws", 86, False
        if 'npm' in args:
            return ('SENTINEL_OUTPUT_LIMIT: output limit exceeded' if failure == 'output' else 'install failed',
                    1, failure == 'timeout')
        if failure == 'alive' and '-e' in args and '/proc' in args[-1]:
            return '', 1, False
        return '', 0, False

    monkeypatch.setattr('snippet_sentinel.runner.command', fake_command)
    monkeypatch.setattr('snippet_sentinel.runner.subprocess.run', lambda *a, **kw: None)
    result = run(Snippet('peer', 'a.md', 1, 'javascript', 'console.log(1)'))
    assert result['status'] == expected, result
    assert sum('worker.cjs' in args for args in calls) == 1
    if failure == 'alive':
        assert not any(args[:3] == ['docker', 'network', 'connect'] for args in calls)


def test_command_output_budget_is_shared(tmp_path):
    budget = [150]
    args = [sys.executable, '-c', "print('x' * 100)"]
    first, _, _ = command(args, tmp_path, 5, budget=budget)
    second, _, _ = command(args, tmp_path, 5, budget=budget)
    assert 'SENTINEL_OUTPUT_LIMIT' not in first
    assert 'SENTINEL_OUTPUT_LIMIT' in second


def test_placeholder_precedes_dns_diagnostic(monkeypatch):
    def fake_command(args, *unused):
        if 'worker.cjs' in args:
            return 'SENTINEL_NETWORK_BLOCKED "example.com"\nTypeError: fetch failed', 1, False
        return '', 0, False

    def unexpected_dns(output):
        pytest.fail('Placeholder failures must not reach the DNS diagnostic')

    monkeypatch.setattr('snippet_sentinel.runner.command', fake_command)
    monkeypatch.setattr('snippet_sentinel.runner.subprocess.run', lambda *a, **kw: None)
    monkeypatch.setattr('snippet_sentinel.runner.dead_host', unexpected_dns)
    result = run(Snippet('placeholder', 'a.md', 1, 'javascript', "await fetch('https://example.com')"))
    assert result['status'] == 'skipped', result
    assert 'placeholder host: example.com' in result['reason']


@pytest.mark.docker
@pytest.mark.skipif(not os.environ.get('SENTINEL_DOCKER_TESTS'), reason='Set SENTINEL_DOCKER_TESTS=1')
@pytest.mark.parametrize('mode', ['commonjs', 'esm', 'typescript'])
@pytest.mark.parametrize('allow_network', [False, True])
def test_docker_missing_sdk_peer_installs_only_between_isolated_attempts(monkeypatch, mode, allow_network):
    connected, attempts, installs = True, [], []
    original = command

    def checked_command(args, *a, **kw):
        nonlocal connected
        if args[:3] == ['docker', 'network', 'disconnect']:
            connected = False
        elif args[:3] == ['docker', 'network', 'connect']:
            connected = True
        if 'npm' in args:
            assert connected
            installs.append(args)
        if 'worker.cjs' in args:
            assert connected == allow_network
            attempts.append(args)
        return original(args, *a, **kw)

    monkeypatch.setattr('snippet_sentinel.runner.command', checked_command)
    prefix = ("import {createRequire} from 'node:module'; const require=createRequire(import.meta.url);\n" if mode == 'esm'
              else 'const n: number = 1;\n' if mode == 'typescript' else '')
    code = prefix + """const fs = require('node:fs');
const assert = require('node:assert');
fs.mkdirSync('node_modules/fixture-sdk', {recursive:true});
fs.writeFileSync('node_modules/fixture-sdk/index.cjs', 'module.exports = require(' + JSON.stringify('ws') + ');');
const sdk = require('./node_modules/fixture-sdk/index.cjs');
assert.equal(typeof sdk.WebSocket, 'function');
if (process.env.SENTINEL_NETWORK_DISABLED === '1') {
 assert(Object.keys(require('node:os').networkInterfaces()).every(name => name === 'lo'));
}
console.log('optional peer loaded offline');
"""
    if not allow_network:
        code += "require('node:assert').equal(process.env.SENTINEL_NETWORK_DISABLED, '1');"
    result = run(Snippet('peer', 'a.md', 1, 'typescript' if mode == 'typescript' else 'javascript', code), allow_network=allow_network)
    assert result['status'] == 'pass', result
    assert 'optional peer loaded offline' in result['stderr']
    assert result['versions']['ws']
    assert len(attempts) == (1 if allow_network else 2)
    assert any(args[-1] == 'ws' for args in installs) == (not allow_network)


@pytest.mark.docker
@pytest.mark.skipif(not os.environ.get('SENTINEL_DOCKER_TESTS'), reason='Set SENTINEL_DOCKER_TESTS=1')
def test_docker_offline_placeholder_is_skipped():
    result = run(Snippet('placeholder', 'a.md', 1, 'javascript', "await fetch('https://example.com')"))
    assert result['status'] == 'skipped', result
    assert 'placeholder host: example.com' in result['reason']
