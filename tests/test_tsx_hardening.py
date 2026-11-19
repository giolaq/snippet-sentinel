import json
import os

import pytest

from snippet_sentinel.dependencies import TSX
from snippet_sentinel.extractor import Snippet
from snippet_sentinel.runner import run


def test_transpiler_installed_before_isolation(monkeypatch):
    calls = []

    def fake_command(args, *unused, **kwargs):
        calls.append(args)
        return ('', 0, False)

    monkeypatch.setattr('snippet_sentinel.runner.command', fake_command)
    monkeypatch.setattr('snippet_sentinel.runner.subprocess.run', lambda *args, **kwargs: None)
    result = run(Snippet('ts', 'a.md', 1, 'typescript', 'const value: number = 1;'))
    assert result['status'] == 'pass'
    install = next(i for i, args in enumerate(calls) if 'npm' in args and 'install' in args)
    disconnect = next(i for i, args in enumerate(calls) if args[:3] == ['docker', 'network', 'disconnect'])
    execute = next(i for i, args in enumerate(calls) if 'worker.cjs' in args)
    assert install < disconnect < execute
    assert TSX in calls[install]
    assert 'NODE_OPTIONS=--require=/work/network_guard.cjs' in calls[execute]
    assert json.loads(calls[execute][-1])[4:] == [True, True, True]
    assert '--read-only' in calls[0]
    assert '/work:rw,exec,nosuid,size=128m' in calls[0]


@pytest.mark.docker
@pytest.mark.skipif(not os.environ.get('SENTINEL_DOCKER_TESTS'), reason='Set SENTINEL_DOCKER_TESTS=1')
@pytest.mark.parametrize('lang,code,expected', [
    ('typescript', "const url: string = 'https://nodejs.org'; await fetch(url);", 'needs_network'),
    ('typescript', "const React = {createElement: () => ({})}; const el = <b />; await fetch('https://nodejs.org');", 'needs_network'),
    ('javascript', "const React = {createElement: () => ({})}; const el = <b />; await fetch('https://nodejs.org');", 'needs_network'),
    ('typescript', "const n: number = 1; require('node:https').get('https://nodejs.org').on('error',()=>setTimeout(()=>{},60000));", 'needs_network'),
    ('typescript', "import fs from 'node:fs'; const n: number = 1; for(let i=0;i<1024;i++) fs.writeSync(1,'x'.repeat(1048576));", 'output_limit'),
    ('typescript', "const fs = require('node:fs'); const n: number = 1; for(let i=0;i<1024;i++) fs.writeSync(2,'x'.repeat(1048576));", 'output_limit'),
    ('typescript', "const fs = require('node:fs'); const n: number = 1; fs.writeFileSync('/work/fill', Buffer.alloc(160*1024*1024));", 'disk_limit'),
    ('typescript', "const url: string = 'https://dead-docs.invalid'; await fetch(url);", 'runtime_error'),
])
def test_tsx_hardening(lang, code, expected):
    result = run(Snippet('ts', 'a.md', 1, lang, code), timeout=20 if '60000' in code else 60)
    assert result['status'] == expected, (result['status'], result['exit_code'], result['stderr'][-2000:])
    if expected == 'needs_network':
        assert 'SENTINEL_NETWORK_BLOCKED' in result['stderr']
    if 'dead-docs.invalid' in code:
        assert 'host dead-docs.invalid does not resolve (dead URL in docs)' in result['stderr']
