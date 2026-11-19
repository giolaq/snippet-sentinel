import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from snippet_sentinel.classifier import classify
from snippet_sentinel.extractor import Snippet
from snippet_sentinel.runner import command, run
from snippet_sentinel.storage import Store


@pytest.mark.parametrize('code,expected,expired', [
    ('', '', False),
    ("print('hello')", 'hello\n', False),
    ("raise RuntimeError('broken')", 'RuntimeError: broken', False),
    ('while True: pass', '', True),
    ("import os\nfor _ in range(1024): os.write(1,b'x'*1048576)", 'output limit exceeded', False),
])
def test_bounded_capture(tmp_path, code, expected, expired):
    out, rc, timed_out = command([sys.executable, '-c', code], tmp_path, 1)
    assert expected in out
    assert timed_out == expired
    assert len(out) < 17000
    if 'output limit' in expected:
        assert classify(out, rc, timed_out) == 'output_limit'


@pytest.mark.parametrize('message,status', [
    ('OSError: [Errno 101] Network is unreachable', 'needs_network'),
    ('Error: connect ENETUNREACH', 'needs_network'),
    ('getaddrinfo EAI_AGAIN example.com', 'needs_network'),
    ('No space left on device', 'disk_limit'),
    ('EROFS: read-only file system', 'disk_limit'),
    ('ordinary failure', 'runtime_error'),
])
def test_resource_classification(message, status):
    assert classify(message, 1, network_disabled=True) == status
    assert classify(message, 0, network_disabled=True) == 'pass'
    if status == 'needs_network':
        assert classify(message, 1) == 'runtime_error'


def test_network_excluded_from_score(tmp_path):
    store = Store(tmp_path / 'score.db')
    rid = store.start('repo')
    store.finish(rid, [{'id': str(i), 'status': status, 'versions': {}}
                       for i, status in enumerate(['needs_network', 'needs_credentials', 'skipped', 'pass', 'disk_limit', 'output_limit'])], {})
    board = store.leaderboard()[0]
    assert board['runnable'] == 3
    assert board['broken'] == 2
    rid = store.start('blocked')
    store.finish(rid, [{'id': 'x', 'status': 'needs_network', 'versions': {}}], {})
    assert store.leaderboard()[-1]['runnable'] == 0


def test_cli_network_opt_in(monkeypatch, tmp_path):
    from snippet_sentinel.cli import main

    calls = []
    monkeypatch.setattr('snippet_sentinel.cli.install_signal_handlers', lambda: None)
    monkeypatch.setattr('snippet_sentinel.cli.scan', lambda *args, **kwargs: calls.append(kwargs) or
                        dict(id=1, repo='repo', ref='', counts={}, stats={}))
    for flags in ([], ['--allow-network']):
        monkeypatch.setattr(sys, 'argv', ['snippet-sentinel', 'scan', str(tmp_path), *flags])
        main()
    assert [c['allow_network'] for c in calls] == [False, True]


def test_docker_isolation_failure_is_closed(monkeypatch):
    calls = []

    def fake_command(args, *unused, **kwargs):
        calls.append(args)
        return ('cannot disconnect', 1, False) if args[:3] == ['docker', 'network', 'disconnect'] else ('', 0, False)

    monkeypatch.setattr('snippet_sentinel.runner.command', fake_command)
    monkeypatch.setattr('snippet_sentinel.runner.subprocess.run', lambda *a, **k: None)
    from snippet_sentinel.runner import InfrastructureError
    with pytest.raises(InfrastructureError, match='network isolation failed'):
        run(Snippet('x', 'a.md', 1, 'python', 'print(1)'))
    assert not any(c[:2] == ['docker', 'exec'] and 'worker.py' in c for c in calls)
    create = calls[0]
    assert '--read-only' in create
    assert create[create.index('--log-driver') + 1] == 'none'
    assert '/work:rw,exec,nosuid,size=128m' in create


def test_dashboard_does_not_enable_network(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from snippet_sentinel.web import create_app

    calls = []
    monkeypatch.setattr('snippet_sentinel.web.scan', lambda *a, **kw: calls.append(kw))
    with TestClient(create_app(tmp_path / 'web.db')) as client:
        response = client.post('/scan', data={'url': 'https://github.com/a/b', 'allow_network': 'true'}, follow_redirects=False)
    assert response.status_code == 303
    assert calls and not calls[0].get('allow_network', False)


@pytest.mark.parametrize('state,removed', [('true', False), ('false', True), ('missing', True), ('daemon error', False)])
def test_cleanup_foreign_dashboard(monkeypatch, state, removed):
    from snippet_sentinel.runner import cleanup_stale

    calls = []
    labels = {'snippet-sentinel.namespace': 'foreign', 'snippet-sentinel.owner-container': 'parent'}

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[1] == 'ps':
            return subprocess.CompletedProcess(args, 0, 'sandbox\n', '')
        if args[-1] == 'sandbox' and args[1] == 'inspect':
            return subprocess.CompletedProcess(args, 0, json.dumps(labels), '')
        if args[-1] == 'parent':
            error = 'No such object: parent' if state == 'missing' else state
            return subprocess.CompletedProcess(args, 0 if state in ['true', 'false'] else 1, state, error)
        return subprocess.CompletedProcess(args, 0, '', '')

    monkeypatch.setattr('snippet_sentinel.runner.subprocess.run', fake_run)
    cleanup_stale()
    assert (['docker', 'rm', '-f', 'sandbox'] in calls) == removed


docker = pytest.mark.skipif(not os.environ.get('SENTINEL_DOCKER_TESTS'), reason='Set SENTINEL_DOCKER_TESTS=1')


@pytest.mark.docker
@docker
@pytest.mark.parametrize('lang', ['python', 'javascript'])
@pytest.mark.parametrize('case', ['pass', 'empty', 'stdout', 'stderr', 'disk', 'readonly', 'timeout'])
def test_docker_limits(lang, case):
    codes = {
        'pass': ("print('hello')", "console.log('hello')"),
        'empty': ('', ''),
        'stdout': ("import os\nfor _ in range(1024): os.write(1,b'x'*1048576)", "const fs=require('node:fs');for(let i=0;i<1024;i++)fs.writeSync(1,Buffer.alloc(1048576,120))"),
        'stderr': ("import os\nfor _ in range(1024): os.write(2,b'x'*1048576)", "const fs=require('node:fs');for(let i=0;i<1024;i++)fs.writeSync(2,Buffer.alloc(1048576,120))"),
        'disk': ("with open('/work/fill','wb') as f:\n for _ in range(160): f.write(b'x'*1048576)", "const fs=require('node:fs');const f=fs.openSync('/work/fill','w');for(let i=0;i<160;i++)fs.writeSync(f,Buffer.alloc(1048576))"),
        'readonly': ("open('/fill','w').write('x')", "require('node:fs').writeFileSync('/fill','x')"),
        'timeout': ('while True: pass', 'while(true){}'),
    }
    code = codes[case][lang == 'javascript']
    result = run(Snippet('limit', 'a.md', 1, lang, code), timeout=3 if case == 'timeout' else 30)
    expected = {'pass': 'pass', 'empty': 'pass', 'stdout': 'output_limit', 'stderr': 'output_limit',
                'disk': 'disk_limit', 'readonly': 'disk_limit', 'timeout': 'timeout'}[case]
    assert result['status'] == expected, result
    if expected == 'output_limit':
        assert 'output limit exceeded' in result['stderr']


@pytest.mark.docker
@docker
@pytest.mark.parametrize('lang', ['python', 'javascript'])
def test_docker_network_and_install(lang):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'host-service')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('0.0.0.0', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        gateway = subprocess.check_output(['docker', 'network', 'inspect', '-f', '{{(index .IPAM.Config 0).Gateway}}', 'bridge'], text=True).strip()
        url = f'http://{gateway}:{server.server_port}/'
        code = (f"import requests\nprint(requests.get('{url}',timeout=2).text)" if lang == 'python' else
                f"const n=require('is-number');require('node:http').get('{url}',r=>r.pipe(process.stdout)).on('error',e=>{{console.error(e);process.exitCode=1}})")
        pins = ['requests==2.32.3'] if lang == 'python' else ['is-number@7.0.0']
        for allowed in [False, True]:
            result = run(Snippet('net', 'a.md', 1, lang, code), timeout=60, pins=pins, allow_network=allowed)
            assert result['status'] == ('pass' if allowed else 'needs_network'), result
            assert result['versions'], result
            if allowed:
                assert 'host-service' in result['stderr']
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def owned_containers(pid):
    names = subprocess.check_output(['docker', 'ps', '-aq', '--filter', 'label=snippet-sentinel.sandbox=true'], text=True).split()
    if not names:
        return []
    inspected = subprocess.run(['docker', 'inspect', *names], capture_output=True, text=True)
    data = json.loads(inspected.stdout or '[]')
    return [c['Id'] for c in data if c['Config']['Labels'].get('snippet-sentinel.owner', '').startswith(str(pid) + ':')]


@pytest.mark.docker
@docker
@pytest.mark.parametrize('sig', [signal.SIGINT, signal.SIGTERM, signal.SIGKILL])
def test_scanner_cleanup(tmp_path, sig):
    from snippet_sentinel.runner import cleanup_stale

    (tmp_path / 'a.md').write_text('```python\nimport time\ntime.sleep(1000)\n```')
    cli = str(Path(sys.executable).with_name('snippet-sentinel'))
    proc = subprocess.Popen([cli, 'scan', str(tmp_path), '--timeout', '120', '--db', str(tmp_path / 'scan.db')],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 15
        while not owned_containers(proc.pid):
            assert proc.poll() is None
            assert time.monotonic() < deadline
            time.sleep(.1)
        cleanup_stale()
        assert owned_containers(proc.pid), 'must not reap a live concurrent scan'
        proc.send_signal(sig)
        proc.communicate(timeout=20)
        if sig == signal.SIGKILL:
            empty = tmp_path / 'empty'
            empty.mkdir()
            subprocess.run([cli, 'scan', str(empty), '--db', str(tmp_path / 'reap.db')], check=True, capture_output=True, timeout=20)
        assert not owned_containers(proc.pid)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=10)
        names = owned_containers(proc.pid)
        if names:
            subprocess.run(['docker', 'rm', '-f', *names], capture_output=True, timeout=10)
