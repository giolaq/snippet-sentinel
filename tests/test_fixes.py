import json
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from snippet_sentinel.fixer import LLM, Rules, fix_run
from snippet_sentinel.github import GitHub, open_fix_pr, replace_fence
from snippet_sentinel.storage import Store
from snippet_sentinel.web import create_app


@pytest.mark.parametrize('lang,code,error,expected', [
    ('python', 'print(os.name)', "NameError: name 'os' is not defined", 'import os\nprint(os.name)'),
    ('python', 'print "hi"', 'SyntaxError', 'print("hi")'),
    ('python', 'import collections\ncollections.Mapping', "AttributeError: collections has no attribute Mapping", 'import collections.abc\nimport collections\ncollections.abc.Mapping'),
    ('python', 'from collections import Mapping', 'ImportError: collections', 'from collections.abc import Mapping'),
    ('javascript', 'console.log(path.sep)', 'ReferenceError: path is not defined', "import path from 'node:path';\nconsole.log(path.sep)"),
    ('javascript', "const a=require('os'); fs.statSync('.')", 'ReferenceError: fs is not defined', "const fs = require('node:fs');\nconst a=require('os'); fs.statSync('.')"),
    ('javascript', "import os from 'os'; const fs = require('fs');", 'require is not defined in ES module scope', "import os from 'os'; import fs from 'fs';"),
    ('python', '', '', None), ('python', 'x()', "NameError: name 'x' is not defined", None),
    ('javascript', 'x()', 'ReferenceError: x is not defined', None),
])
def test_rules(lang, code, error, expected):
    assert Rules().propose({'lang': lang, 'code': code, 'stderr': error}) == expected


@pytest.mark.parametrize('provider', ['openai', 'anthropic', 'none'])
def test_llm(monkeypatch, provider):
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(
        {'content': [{'text': '```python\nprint(1)\n```'}]} if provider == 'anthropic' else
        {'choices': [{'message': {'content': '```python\nprint(1)\n```'}}]}).encode()
    http = MagicMock(return_value=response)
    monkeypatch.setattr('snippet_sentinel.fixer.urlopen', http)
    if provider != 'none':
        monkeypatch.setenv(provider.upper() + '_API_KEY', 'test-key')
    assert LLM().propose({'lang': 'python', 'code': 'bad', 'stderr': 'error'}) == (None if provider == 'none' else 'print(1)')
    assert http.call_count == (0 if provider == 'none' else 1)


@pytest.mark.parametrize('indent', ['', '  ', '\t'])
def test_fence(indent):
    text = f'heading\n{indent}~~~py\n{indent}print(1)\n{indent}~~~\nend\n'
    fix = {'line': 3, 'original': 'print(1)', 'proposed': 'import os\nprint(os.name)'}
    assert replace_fence(text, fix) == f'heading\n{indent}~~~py\n{indent}import os\n{indent}print(os.name)\n{indent}~~~\nend\n'
    assert replace_fence(text, {**fix, 'original': 'wrong'}) is None
    assert replace_fence(text, {**fix, 'line': 1}) is None
    assert replace_fence('```py\nprint(1)', {**fix, 'line': 2}) is None


def make_run(db, results=True):
    store = Store(db)
    rid = store.start('https://github.com/a/b')
    result = {'id': 'x', 'file': 'a.md', 'line': 2, 'lang': 'python', 'code': 'print(os.name)',
              'status': 'runtime_error', 'stderr': "NameError: name 'os' is not defined", 'runner': 'docker', 'versions': {}}
    store.finish(rid, [result] if results else [], {}, 'a' * 40)
    return store, rid


@pytest.mark.parametrize('outcome', ['pass', 'runtime_error', 'exception', 'empty'])
def test_verification(tmp_path, monkeypatch, outcome):
    db = tmp_path / 's.db'
    store, rid = make_run(db, outcome != 'empty')
    fake = MagicMock(return_value={'status': outcome, 'stderr': 'tail'})
    if outcome == 'exception':
        fake.side_effect = RuntimeError('sandbox unavailable')
    monkeypatch.setattr('snippet_sentinel.fixer.runner.run', fake)
    job = fix_run(rid, db, [Rules()])
    assert job['status'] == 'done'
    if outcome == 'empty':
        assert job['results'] == []
        fake.assert_not_called()
    else:
        fix = job['results'][0]
        assert fix['verified'] == (outcome == 'pass')
        assert fix['original'] == 'print(os.name)'
        assert fix['output'] == ('sandbox unavailable' if outcome == 'exception' else 'tail')
        assert fake.call_args.args[1] == 'docker'
    assert fix_run(rid, db, [Rules()]) == job
    with pytest.raises(ValueError):
        fix_run(999, db)


@pytest.mark.parametrize('fork', [False, True])
def test_github_flow(tmp_path, fork):
    client = GitHub(token='test-token', api_base='https://api.test')
    calls = []
    def git(root, *args, **kwargs):
        calls.append(args)
        if args[0] == 'clone':
            (root / 'a.md').write_text('  ```py\n  old\n  ```\n')
        if args[0] == 'push' and args[1] == 'https://github.com/a/b' and fork:
            raise PermissionError('403')
        return ''
    client.git = git
    def api(path, data=None):
        if path.endswith('/forks'):
            return {'full_name': 'me/b', 'owner': {'login': 'me'}}
        if path.endswith('/pulls'):
            assert data['base'] == 'main'
            assert data['head'] == ('me:sentinel/fix-1' if fork else 'sentinel/fix-1')
            assert 'a.md:2' in data['body'] and 'rules' in data['body'] and 'before' in data['body']
            return {'html_url': 'https://github.com/a/b/pull/1'}
        return {'default_branch': 'main'}
    client.api = api
    run = {'id': 1, 'repo': 'https://github.com/a/b', 'ref': 'a' * 40}
    fix = {'file': 'a.md', 'line': 2, 'original': 'old', 'proposed': 'new', 'verified': True, 'proposer': 'rules', 'error_before': 'before'}
    assert client.open_pr(run, [fix])[0].endswith('/1')
    assert ('checkout', '-b', 'sentinel/fix-1', 'a' * 40) in calls
    with pytest.raises(ValueError, match='No verified'):
        client.open_pr(run, [{**fix, 'verified': False}])
    with pytest.raises(ValueError, match='mismatch'):
        client.open_pr(run, [{**fix, 'original': 'wrong'}])


def test_api_and_web(tmp_path, monkeypatch):
    response = MagicMock()
    response.__enter__.return_value.read.return_value = b'{"default_branch":"main"}'
    http = MagicMock(return_value=response)
    monkeypatch.setattr('snippet_sentinel.github.urlopen', http)
    assert GitHub(token='test', api_base='https://api.test').api('/repos/a/b')['default_branch'] == 'main'
    assert http.call_args.args[0].full_url == 'https://api.test/repos/a/b'
    monkeypatch.delenv('GITHUB_TOKEN', raising=False)
    monkeypatch.delenv('GH_TOKEN', raising=False)
    db = tmp_path / 's.db'
    store, rid = make_run(db)
    monkeypatch.setattr('snippet_sentinel.fixer.runner.run', lambda *a: {'status': 'pass', 'stderr': ''})
    web = TestClient(create_app(db))
    assert 'Propose fixes' in web.get(f'/runs/{rid}').text
    page = web.post(f'/runs/{rid}/fix').text
    assert 'Verified' in page and 'Open fix PR' in page and '--- before' in page
    page = web.post(f'/runs/{rid}/fix/pr').text
    assert 'Set GITHUB_TOKEN' in page
    assert store.fix_job(rid)['status'] == 'error'
    fake = MagicMock()
    fake.open_pr.return_value = ('https://github.com/a/b/pull/1', [])
    assert open_fix_pr(rid, db, fake)['pr_url'].endswith('/1')
    open_fix_pr(rid, db, fake)
    assert fake.open_pr.call_count == 1
    assert web.post('/runs/999/fix').status_code == 404
    assert web.post('/runs/999/fix/pr').status_code == 400
