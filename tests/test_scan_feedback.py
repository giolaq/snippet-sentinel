import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from html import unescape

import pytest
from fastapi.testclient import TestClient

from snippet_sentinel.cli import main
from snippet_sentinel.extractor import Snippet
from snippet_sentinel.scan import git_error, redact_git_error, scan
from snippet_sentinel.storage import Store
from snippet_sentinel.web import create_app, render_run


def passed(snippet, *args):
    return {**snippet.dict(), 'status': 'pass', 'versions': {}, 'runner': 'docker', 'exit_code': 0, 'stderr': ''}


@pytest.mark.parametrize('stderr,ref,expected', [
    (b'remote: Repository not found.', None, 'Repository not found or not accessible'),
    (b"fatal: repository 'https://github.com/a/b/' not found", None, 'Repository not found or not accessible'),
    (b"fatal: could not read Username for 'https://github.com': terminal prompts disabled", None, "private repos aren't supported / check the URL"),
    (b'fatal: Authentication failed', None, 'Repository not found or not accessible'),
    (b'fatal: Remote branch absent not found in upstream origin', 'absent', "Branch or tag 'absent' not found"),
    (b"fatal: couldn't find remote ref absent", 'absent', "Branch or tag 'absent' not found"),
    (b'fatal: Could not resolve host: github.com', None, 'network/DNS failure'),
    (b'fatal: Failed to connect to github.com port 443: Connection refused', None, 'network/DNS failure'),
    (b'fatal: SSL certificate problem: certificate has expired', None, 'SSL certificate problem: certificate has expired'),
    (b'', None, 'git returned no error details'),
])
def test_git_failure_cli_and_run_page(tmp_path, monkeypatch, capsys, stderr, ref, expected):
    db = tmp_path / 'scan.db'

    def fail(args, **kwargs):
        assert kwargs['env']['GIT_TERMINAL_PROMPT'] == '0'
        raise subprocess.CalledProcessError(128, args, stderr=stderr)

    monkeypatch.setattr('snippet_sentinel.scan.subprocess.run', fail)
    argv = ['snippet-sentinel', 'scan', 'https://github.com/a/b', '--db', str(db)]
    if ref:
        argv += ['--ref', ref]
    monkeypatch.setattr('sys.argv', argv)
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1
    output = capsys.readouterr()
    assert expected in output.err and not output.out
    stored = Store(db).get(1)
    assert stored['finished'] and expected in stored['error']
    assert expected in unescape(TestClient(create_app(db)).get('/runs/1').text)


def test_git_timeout_is_clear_and_persisted(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired(['git', 'clone'], 120)

    monkeypatch.setattr('snippet_sentinel.scan.subprocess.run', fail)
    db = tmp_path / 'scan.db'
    with pytest.raises(ValueError, match='Git clone timed out after 120 seconds'):
        scan('https://github.com/a/b', db=db)
    assert 'network connection' in Store(db).get(1)['error']


def test_git_diagnostics_are_redacted(tmp_path, monkeypatch, capsys):
    secrets = ['test-password', 'ghp_sentinelDummy', 'github_pat_sentinelDummy', 'test-token', 'test-secret', 'dGVzdC1vbmx5']
    stderr = ("fatal: unable to access 'https://user:test-password@github.com/a/b?access_token=test-token':\n"
              'Authorization: Basic dGVzdC1vbmx5\npassword=test-password token=test-token api_key="test-secret"\n'
              'ghp_sentinelDummy github_pat_sentinelDummy\nfatal: TLS handshake failed')
    output = redact_git_error(stderr.encode())
    assert all(secret not in output for secret in secrets)
    assert 'TLS handshake failed' in output
    assert 'test-password' not in redact_git_error('socks5://user:test-password@proxy')
    assert 'ghp_sentinelDummy' not in git_error('Remote branch absent not found', 'ghp_sentinelDummy')

    def fail(args, **kwargs):
        raise subprocess.CalledProcessError(128, args, stderr=stderr.encode())

    monkeypatch.setattr('snippet_sentinel.scan.subprocess.run', fail)
    db = tmp_path / 'scan.db'
    monkeypatch.setattr('sys.argv', ['snippet-sentinel', 'scan', 'https://github.com/a/b', '--db', str(db)])
    with pytest.raises(SystemExit):
        main()
    persisted = Store(db).get(1)['error']
    page = TestClient(create_app(db)).get('/runs/1').text
    combined = capsys.readouterr().err + persisted + page
    assert all(secret not in combined for secret in secrets)


@pytest.mark.parametrize('source', ['github.com/foo', 'www.github.com/owner/repo', 'git@github.com:owner/repo.git'])
def test_url_like_paths_get_https_hint(tmp_path, source):
    with pytest.raises(ValueError, match='https://github.com/owner/repo'):
        scan(source, db=tmp_path / 'scan.db')


def test_plain_missing_path_keeps_local_error(tmp_path):
    with pytest.raises(ValueError, match='Local repository directory does not exist'):
        scan(str(tmp_path / 'missing'), db=tmp_path / 'scan.db')


@pytest.mark.parametrize('cap,selected', [(500, 500), (2, 2), (0, 501)])
def test_cap_and_partial_report(tmp_path, monkeypatch, cap, selected):
    (tmp_path / 'a.md').write_text(''.join(f'```python\nprint({i})\n```\n' for i in range(501)))
    monkeypatch.setattr('snippet_sentinel.scan.run', passed)
    messages = []
    result = scan(str(tmp_path), db=tmp_path / 'scan.db', max_snippets=cap, progress=messages.append)
    assert result['counts'] == {'pass': selected}
    assert result['stats'] == {'unsupported': 0, 'found': 501, 'selected': selected, 'truncated': 501 - selected, 'max_snippets': cap}
    assert messages[0] == 'Found 501 snippets in 1 files'
    assert messages[-1] == f'{selected}/{selected} done'
    assert ('Snippet limit reached' in '\n'.join(messages)) == (selected < 501)
    page = render_run(result)
    assert ('Partial scan:' in page) == (selected < 501)


def test_language_filter_precedes_cap(tmp_path, monkeypatch):
    (tmp_path / 'a.md').write_text('```javascript\n1\n```\n```python\n1\n```\n```python\n2\n```')
    monkeypatch.setattr('snippet_sentinel.scan.run', passed)
    result = scan(str(tmp_path), db=tmp_path / 'scan.db', lang='python', max_snippets=1)
    assert result['stats']['found'] == 2 and result['stats']['truncated'] == 1
    assert [s['lang'] for s in result['results']] == ['python']


def test_empty_scan_and_invalid_cap(tmp_path):
    messages = []
    result = scan(str(tmp_path), db=tmp_path / 'scan.db', progress=messages.append)
    assert result['results'] == []
    assert messages == ['Found 0 snippets in 0 files', '0/0 done']
    with pytest.raises(ValueError, match='nonnegative'):
        scan(str(tmp_path), db=tmp_path / 'scan.db', max_snippets=-1)


def test_completion_progress_does_not_wait_for_input_order(tmp_path, monkeypatch):
    (tmp_path / 'a.md').write_text('```python\nprint(0)\n```\n```python\nprint(1)\n```')
    release = threading.Event()
    messages = []

    def fake_run(snippet, *args):
        if snippet.code == 'print(0)':
            assert release.wait(5), 'progress waited for the first snippet instead of completed snippets'
        return passed(snippet)

    def progress(message):
        messages.append(message)
        if message == '1/2 done':
            release.set()

    monkeypatch.setattr('snippet_sentinel.scan.run', fake_run)
    result = scan(str(tmp_path), db=tmp_path / 'scan.db', progress=progress, jobs=2)
    assert messages[-2:] == ['1/2 done', '2/2 done']
    assert [s['code'] for s in result['results']] == ['print(0)', 'print(1)']


def test_cli_progress_stderr_and_json_file(tmp_path, monkeypatch, capsys):
    (tmp_path / 'a.md').write_text('```python\n1\n```\n```python\n2\n```')
    monkeypatch.setattr('snippet_sentinel.scan.run', passed)
    report = tmp_path / 'report.json'
    monkeypatch.setattr('sys.argv', ['snippet-sentinel', 'scan', str(tmp_path), '--runner', 'local',
                                  '--max-snippets', '1', '--json', str(report), '--db', str(tmp_path / 'scan.db')])
    main()
    output = capsys.readouterr()
    assert 'Found 2 snippets in 1 files' in output.err
    assert '1/1 done' in output.err and 'Snippet limit reached' in output.err
    assert 'UNSANDBOXED' in output.err
    assert 'Found' not in output.out and 'done' not in output.out and 'UNSANDBOXED' not in output.out
    assert json.loads(report.read_text())['stats']['selected'] == 1


def test_cli_rejects_negative_cap(monkeypatch, capsys):
    monkeypatch.setattr('sys.argv', ['snippet-sentinel', 'scan', '.', '--max-snippets', '-1'])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert 'must be nonnegative' in capsys.readouterr().err


def test_dashboard_deduplicates_pending_url_and_ref(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr('snippet_sentinel.web.scan', lambda *args, **kwargs: calls.append(kwargs))
    db = tmp_path / 'scan.db'
    client = TestClient(create_app(db))
    first = client.post('/scan', data={'url': 'https://github.com/a/b.git', 'ref': 'main'}, follow_redirects=False)
    duplicate = client.post('/scan', data={'url': 'https://github.com/A/B', 'ref': 'main'}, follow_redirects=False)
    other = client.post('/scan', data={'url': 'https://github.com/a/b', 'ref': 'other'}, follow_redirects=False)
    assert first.status_code == duplicate.status_code == other.status_code == 303
    assert first.headers['location'] == duplicate.headers['location'] != other.headers['location']
    assert [c['ref'] for c in calls] == ['main', 'other']
    Store(db).finish(1, [], {}, ref='commit', error='failed')
    new = client.post('/scan', data={'url': 'https://github.com/a/b', 'ref': 'main'}, follow_redirects=False)
    assert new.headers['location'] != first.headers['location']
    assert len(calls) == 3


def test_dashboard_pending_creation_is_atomic(tmp_path):
    store = Store(tmp_path / 'scan.db')
    with ThreadPoolExecutor(max_workers=8) as pool:
        answers = list(pool.map(lambda _: store.start_or_reuse('https://github.com/a/b', 'main'), range(16)))
    assert len({run_id for run_id, created in answers}) == 1
    assert sum(created for run_id, created in answers) == 1
    assert store.start_or_reuse('https://github.com/a/b', 'Main')[1]
    assert store.start_or_reuse('https://github.com/a/c', 'main')[1]


def test_dashboard_uses_default_cap(tmp_path, monkeypatch):
    snippets = [Snippet(str(i), 'a.md', i + 1, 'python', str(i)) for i in range(501)]
    monkeypatch.setattr('snippet_sentinel.scan.extract', lambda path: (snippets, {'unsupported': 0}))
    monkeypatch.setattr('snippet_sentinel.scan.run', passed)
    monkeypatch.setattr('snippet_sentinel.scan.subprocess.run', lambda args, **kwargs: subprocess.CompletedProcess(args, 0, '', ''))
    db = tmp_path / 'scan.db'
    client = TestClient(create_app(db))
    response = client.post('/scan', data={'url': 'https://github.com/a/b'}, follow_redirects=False)
    assert response.status_code == 303
    stored = Store(db).get(1)
    assert stored['stats']['selected'] == 500 and stored['stats']['truncated'] == 1
    page = client.get(response.headers['location']).text
    assert 'Partial scan: checked the first 500 of 501 snippets' in page
