import json
import os
import sys

import pytest

from snippet_sentinel.extractor import Snippet
from snippet_sentinel.runner import InfrastructureError, command, run


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
        if args[:2] == ['docker', 'create']:
            connected = True
        if args[:3] == ['docker', 'network', 'disconnect']:
            connected = False
        elif args[:3] == ['docker', 'network', 'connect']:
            connected = True
        if '/usr/local/bin/npm' in args:
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
    assert [args[args.index('--') + 1:] for args in calls if '/usr/local/bin/npm' in args] == [installs[:i + 1] for i in range(len(installs))]
    created = [args[args.index('--name') + 1] for args in calls if args[:2] == ['docker', 'create']]
    assert len(created) == len(installs) + 1
    assert len(set(created)) == len(created)
    assert not any(args[:3] == ['docker', 'network', 'connect'] for args in calls)
    assert all(budget is budgets[0] for budget in budgets)
    assert limits == sorted(limits, reverse=True)
    if expected == 'pass':
        assert result['versions'] == {p: '1.0.0' for p in installs}


@pytest.mark.parametrize('failure,expected', [('install', 'missing_dependency'), ('timeout', 'timeout'),
                                            ('output', 'output_limit')])
def test_failed_peer_setup_never_reexecutes_snippet(monkeypatch, failure, expected):
    calls = []

    def fake_command(args, *unused):
        calls.append(args)
        if 'worker.cjs' in args:
            return "Error: Cannot find module 'ws'\nSENTINEL_NEED_PEER=ws", 86, False
        if '/usr/local/bin/npm' in args:
            return ('SENTINEL_OUTPUT_LIMIT: output limit exceeded' if failure == 'output' else 'install failed',
                    1, failure == 'timeout')
        return '', 0, False

    monkeypatch.setattr('snippet_sentinel.runner.command', fake_command)
    monkeypatch.setattr('snippet_sentinel.runner.subprocess.run', lambda *a, **kw: None)
    result = run(Snippet('peer', 'a.md', 1, 'javascript', 'console.log(1)'))
    assert result['status'] == expected, result
    assert sum('worker.cjs' in args for args in calls) == 1
    assert not any(args[:3] == ['docker', 'network', 'connect'] for args in calls)


def test_command_output_budget_is_shared(tmp_path):
    budget = [150]
    args = [sys.executable, '-c', "print('x' * 100)"]
    first, _, _ = command(args, tmp_path, 5, budget=budget)
    second, _, _ = command(args, tmp_path, 5, budget=budget)
    assert 'SENTINEL_OUTPUT_LIMIT' not in first
    assert 'SENTINEL_OUTPUT_LIMIT' in second


def test_peer_removal_failure_never_creates_a_networked_replacement(monkeypatch):
    calls = []

    def fake_command(args, *unused):
        calls.append(args)
        if 'worker.cjs' in args:
            return 'SENTINEL_NEED_PEER=ws', 86, False
        if args[:3] == ['docker', 'rm', '-f']:
            return 'removal refused', 1, False
        return '', 0, False

    monkeypatch.setattr('snippet_sentinel.runner.command', fake_command)
    monkeypatch.setattr('snippet_sentinel.runner.subprocess.run', lambda *a, **kw: None)
    result = run(Snippet('peer', 'a.md', 1, 'javascript', 'console.log(1)'))
    assert result['status'] == 'runtime_error', result
    assert 'removal refused' in result['stderr']
    assert sum(args[:2] == ['docker', 'create'] for args in calls) == 1


@pytest.mark.parametrize('stage', ['before_create', 'remove', 'create', 'start', 'copy', 'install', 'disconnect', 'run'])
def test_peer_retry_deadline_is_a_snippet_timeout_with_previous_output(monkeypatch, stage):
    calls, cleanups = [], []
    clock = [0]
    attempts = 0

    def fake_command(args, *unused):
        nonlocal attempts
        calls.append(args)
        if args[:2] == ['docker', 'create']:
            attempts += 1
        if attempts == 1 and 'worker.cjs' in args:
            return 'earlier snippet output\nSENTINEL_VERSIONS={"sdk":"1"}\nSENTINEL_NEED_PEER=ws', 86, False
        if stage == 'before_create' and args[:3] == ['docker', 'rm', '-f']:
            clock[0] = 100
            return '', 0, False
        timed_stage = ((stage == 'remove' and args[:3] == ['docker', 'rm', '-f']) or
                       (attempts == 2 and ((stage == 'create' and args[:2] == ['docker', 'create']) or
                        (stage == 'start' and args[:2] == ['docker', 'start']) or
                        (stage == 'copy' and '/bin/tar' in args) or
                        (stage == 'install' and '/usr/local/bin/npm' in args) or
                        (stage == 'disconnect' and args[:3] == ['docker', 'network', 'disconnect']) or
                        (stage == 'run' and 'worker.cjs' in args))))
        return ('retry diagnostic', -9, True) if timed_stage else ('', 0, False)

    monkeypatch.setattr('snippet_sentinel.runner.time.monotonic', lambda: clock[0])
    monkeypatch.setattr('snippet_sentinel.runner.command', fake_command)
    monkeypatch.setattr('snippet_sentinel.runner.subprocess.run', lambda args, **kw: cleanups.append(args))
    result = run(Snippet('peer', 'a.md', 1, 'javascript', 'console.log(1)'), timeout=60)
    assert result['status'] == 'timeout', result
    assert 'earlier snippet output' in result['stderr']
    assert 'Optional-peer retry timed out' in result['stderr']
    assert cleanups
    if stage != 'before_create':
        assert 'retry diagnostic' in result['stderr']


@pytest.mark.parametrize('retry', [False, True])
def test_only_initial_create_failure_is_infrastructure(monkeypatch, retry):
    attempts = 0

    def fake_command(args, *unused):
        nonlocal attempts
        if args[:2] == ['docker', 'create']:
            attempts += 1
            if attempts == (2 if retry else 1):
                return 'Docker create failed', 1, False
        if 'worker.cjs' in args:
            return 'earlier snippet output\nSENTINEL_NEED_PEER=ws', 86, False
        return '', 0, False

    monkeypatch.setattr('snippet_sentinel.runner.command', fake_command)
    monkeypatch.setattr('snippet_sentinel.runner.subprocess.run', lambda *a, **kw: None)
    if retry:
        result = run(Snippet('peer', 'a.md', 1, 'javascript', 'console.log(1)'))
        assert result['status'] == 'runtime_error', result
        assert 'earlier snippet output' in result['stderr']
        assert 'Docker create failed' in result['stderr']
    else:
        with pytest.raises(InfrastructureError, match='Docker create failed'):
            run(Snippet('peer', 'a.md', 1, 'javascript', 'console.log(1)'))


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
        if args[:2] == ['docker', 'create']:
            connected = True
        if args[:3] == ['docker', 'network', 'disconnect']:
            connected = False
        elif args[:3] == ['docker', 'network', 'connect']:
            connected = True
        if '/usr/local/bin/npm' in args:
            assert connected
            assert 'PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin' in args
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
    assert any('ws' in args[args.index('--') + 1:] for args in installs) == (not allow_network)


@pytest.mark.docker
@pytest.mark.skipif(not os.environ.get('SENTINEL_DOCKER_TESTS'), reason='Set SENTINEL_DOCKER_TESTS=1')
def test_docker_offline_placeholder_is_skipped():
    result = run(Snippet('placeholder', 'a.md', 1, 'javascript', "await fetch('https://example.com')"))
    assert result['status'] == 'skipped', result
    assert 'placeholder host: example.com' in result['reason']


@pytest.mark.docker
@pytest.mark.skipif(not os.environ.get('SENTINEL_DOCKER_TESTS'), reason='Set SENTINEL_DOCKER_TESTS=1')
@pytest.mark.parametrize('attack', ['npm', 'detached'])
def test_peer_retry_destroys_poisoned_sandbox_before_networked_install(monkeypatch, attack):
    original = command
    attempts, removed = [], []

    def checked_command(args, *a, **kw):
        assert args[:3] != ['docker', 'network', 'connect']
        if 'worker.cjs' in args:
            attempts.append(args[args.index('worker.cjs') - 2])
        if args[:3] == ['docker', 'rm', '-f']:
            assert args[-1] == attempts[0]
            if attack == 'detached':
                probe = "const fs=require('node:fs');const rows=fs.readdirSync('/proc').filter(p=>/^\\d+$/.test(p)).flatMap(p=>{try{return [fs.readFileSync('/proc/'+p+'/stat','utf8')]}catch{return []}});if(!rows.some(s=>s.includes('evil ) Z ')&&!s.slice(s.lastIndexOf(')')+1).trimStart().startsWith('Z')))process.exit(1);if(Object.keys(require('node:os').networkInterfaces()).some(n=>n!=='lo'))process.exit(2);"
                out, rc, expired = original(['docker', 'exec', args[-1], '/usr/local/bin/node', '-e', probe], a[0], 5)
                assert rc == 0 and not expired, out
            removed.append(args[-1])
        if '/usr/local/bin/npm' in args:
            assert removed == attempts[:1]
            assert attempts[0] not in args
            assert 'PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin' in args
            name = args[args.index('/usr/local/bin/npm') - 1]
            out, rc, expired = original(['docker', 'exec', name, '/usr/local/bin/node', '-e', "if(require('node:fs').existsSync('/work/.packages/bin/npm'))process.exit(1)"], a[0], 5)
            assert rc == 0 and not expired, out
            _, rc, _ = original(['docker', 'inspect', attempts[0]], a[0], 5)
            assert rc != 0
        return original(args, *a, **kw)

    monkeypatch.setattr('snippet_sentinel.runner.command', checked_command)
    payload = ("fs.mkdirSync('/work/.packages/bin',{recursive:true});fs.writeFileSync('/work/.packages/bin/npm','#!/bin/sh\\necho SENTINEL_PLANTED_NPM\\n',{mode:0o755});"
               if attack == 'npm' else
               "const child=require('node:child_process').spawn('/usr/local/bin/node',['-e',\"process.title='evil ) Z worker';require('node:fs').writeFileSync('/proc/self/comm','evil ) Z worker');setInterval(()=>{},1000)\"],{detached:true,stdio:'ignore'});child.unref();require('node:child_process').execFileSync('/bin/sleep',['0.2']);")
    code = "const fs=require('node:fs');let missing=false;try{require.resolve('ws')}catch{missing=true}if(missing){" + payload + "}" + """
fs.mkdirSync('node_modules/fixture-sdk',{recursive:true});
fs.writeFileSync('node_modules/fixture-sdk/index.cjs','module.exports = require(' + JSON.stringify('ws') + ');');
const sdk=require('./node_modules/fixture-sdk/index.cjs');
require('node:assert').equal(typeof sdk.WebSocket,'function');
"""
    result = run(Snippet('attack', 'a.md', 1, 'javascript', code))
    assert result['status'] == 'pass', result
    assert len(attempts) == 2 and attempts[0] != attempts[1]
    assert removed == attempts[:1]
    assert 'SENTINEL_PLANTED_NPM' not in result['stderr']
