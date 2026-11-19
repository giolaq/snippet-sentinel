import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from snippet_sentinel.classifier import classify
from snippet_sentinel.dependencies import dependencies, is_esm, pinned
from snippet_sentinel.extractor import extract
from snippet_sentinel.extractor import Snippet
from snippet_sentinel.runner import run
from snippet_sentinel.scan import github_url, scan
from snippet_sentinel.storage import Store
from snippet_sentinel.web import create_app, render_run


def test_extract(tmp_path):
    (tmp_path / 'a.mdx').write_text('  ~~~py\nprint(1)\n  ~~~\n```ts\nx\n```\n<!-- sentinel:skip -->\n```js\nx\n```\n```python norun\nx\n```\n```py\n>>> x\n```\n```js\n// ...\n```\n')
    (tmp_path / 'vendor').mkdir()
    (tmp_path / 'vendor/x.md').write_text('```py\nx\n```')
    snippets, stats = extract(tmp_path)
    assert len(snippets) == 5
    assert snippets[0].line == 2 and snippets[0].lang == 'python'
    assert all(s.reason for s in snippets[1:])
    assert stats == {'unsupported': 1}
    assert extract(tmp_path)[0][0].id == snippets[0].id
    assert extract(tmp_path / 'vendor' / 'absent') == ([], {'unsupported': 0})


@pytest.mark.parametrize('code,lang,expected', [
    ('import os, sys\nfrom PIL import Image\nimport yaml\nfrom . import local', 'python', ['pillow', 'pyyaml']),
    ('def bad(:', 'python', []),
    ("import x from '@scope/sdk/sub'; const fs=require('node:fs'); require('lodash/map'); import('./local.js'); import 'dotenv/config';", 'javascript', ['@scope/sdk', 'dotenv', 'lodash']),
    ('', 'javascript', []), ('', 'python', [])])
def test_dependencies(code, lang, expected):
    assert dependencies(code, lang) == expected


def test_pins_and_esm():
    assert pinned(['requests'], ['requests==2.32.3'], 'python') == ['requests==2.32.3']
    assert pinned(['@a/b'], ['@a/b@1.2.3'], 'javascript') == ['@a/b@1.2.3']
    assert is_esm('await Promise.resolve(1)')
    assert not is_esm("const fs = require('fs')")


@pytest.mark.parametrize('code,esm', [
    ("const fs = require('node:fs'); async function main() { await Promise.resolve(); } main();", False),
    ("const fs = require('node:fs'); const main = async () => { await Promise.resolve(); }; main();", False),
    ("const main = async () => await Promise.resolve(); main();", False),
    ("const fs = require('node:fs'); await Promise.resolve();", False),
    ("if (true) { await Promise.resolve(); }", True),
    ("// import x from 'x'\nconsole.log('await import export');", False),
    ("import('node:fs').then(() => {});", False),
    ("import fs from 'node:fs'; await Promise.resolve();", True),
    ('', False),
])
def test_javascript_module_mode(code, esm):
    assert is_esm(code) is esm


@pytest.mark.skipif(not shutil.which('node'), reason='Local Node required')
@pytest.mark.parametrize('code,expected', [
    ("const fs = require('node:fs'); async function main() { await Promise.resolve(); } main();", 'pass'),
    ("const fs = require('node:fs'); await Promise.resolve();", 'pass'),
    ("const fs = require('node:fs'); await Promise.reject(new Error('broken'));", 'runtime_error'),
])
def test_async_commonjs_execution(code, expected):
    assert run(Snippet('js', 'a.md', 1, 'javascript', code), 'local')['status'] == expected


@pytest.mark.parametrize('message,code,expected', [('', 0, 'pass'), ('SyntaxError: x', 1, 'syntax_error'),
    ('Cannot find module x', 1, 'missing_dependency'), ('401 Unauthorized', 1, 'needs_credentials'),
    ("KeyError: 'API_KEY'", 1, 'needs_credentials'), ('process.env.API_KEY undefined', 1, 'needs_credentials'),
    ('RuntimeError', 1, 'runtime_error'), ('', 1, 'runtime_error')])
def test_classifier(message, code, expected):
    assert classify(message, code) == expected


def test_timeout_install():
    assert classify('', -9, timed_out=True) == 'timeout'
    assert classify('', 1, install_failed=True) == 'missing_dependency'


def test_local_demo(tmp_path):
    run = scan(str(Path(__file__).parents[1] / 'examples/demo-docs'), db=tmp_path / 'scan.db', runner='local', jobs=4, timeout=60)
    assert not run['error']
    assert run['counts']['skipped'] == 5
    assert run['counts']['pass'] >= 1
    assert run['counts']['syntax_error'] >= 1
    assert run['counts']['missing_dependency'] >= 1
    assert run['counts']['runtime_error'] >= 1
    assert run['counts']['needs_credentials'] >= 1
    if shutil.which('node') and shutil.which('npm'):
        assert run['counts'] == {'pass': 2, 'syntax_error': 2, 'missing_dependency': 2, 'runtime_error': 2, 'needs_credentials': 2, 'skipped': 5}
    assert 'UNSANDBOXED' in render_run(run, static=True)


def test_storage_streak_and_web(tmp_path):
    store = Store(tmp_path / 'store.db')
    result = {'id': 'x', 'status': 'runtime_error', 'versions': {'sdk': '1'}}
    for status in ['pass', 'runtime_error', 'runtime_error']:
        rid = store.start('repo')
        store.finish(rid, [{**result, 'status': status}], {})
    assert store.get(3)['results'][0]['broken_since']['run_id'] == 2
    assert store.leaderboard()[0]['percent'] == 100
    rid = store.start('repo')
    store.finish(rid, [], {})
    rid = store.start('repo')
    store.finish(rid, [result], {})
    assert store.get(rid)['results'][0]['broken_since']['run_id'] == rid
    client = TestClient(create_app(tmp_path / 'empty.db'))
    assert client.get('/').status_code == 200
    assert client.get('/runs/999').status_code == 404
    assert client.post('/scan', content='url=https://evil.test').status_code == 400


def test_invalid_repo(tmp_path):
    with pytest.raises(ValueError):
        github_url('https://github.com/a/b/tree/main')
    with pytest.raises(ValueError):
        scan(str(tmp_path / 'absent'), db=tmp_path / 'errors.db')
    assert Store(tmp_path / 'errors.db').get(1)['error']


def test_indented_fence(tmp_path):
    (tmp_path / 'a.md').write_text('  ```python\n  print(1)\n  ```\n')
    assert extract(tmp_path)[0][0].code == 'print(1)'


def test_local_timeout_and_empty():
    snippet = Snippet('x', 'a.md', 2, 'python', 'while True: pass')
    assert run(snippet, 'local', timeout=2)['status'] == 'timeout'
    snippet.code = ''
    assert run(snippet, 'local', timeout=30)['status'] == 'pass'


@pytest.mark.docker
@pytest.mark.skipif(not __import__('os').environ.get('SENTINEL_DOCKER_TESTS'), reason='Set SENTINEL_DOCKER_TESTS=1 for Docker integration')
def test_docker_versions():
    snippet = Snippet('x', 'a.md', 2, 'python', 'import requests\nprint(requests.__version__)')
    result = run(snippet, pins=['requests==2.32.3'])
    assert result['status'] == 'pass'
    assert result['versions']['requests'] == '2.32.3'
