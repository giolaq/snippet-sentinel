import re
import shutil
from html import unescape
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from snippet_sentinel.classifier import classify
from snippet_sentinel.dependencies import dependencies, is_esm, pinned
from snippet_sentinel.extractor import extract
from snippet_sentinel.extractor import Snippet
from snippet_sentinel.runner import InfrastructureError, run
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


@pytest.mark.parametrize('code,expected', [
    ('from google.cloud import storage', ['google-cloud-storage']),
    ('import google.cloud.storage', ['google-cloud-storage']),
    ('from google.cloud.bigquery_storage import BigQueryReadClient', ['google-cloud-bigquery-storage']),
    ('from google.cloud import storage, bigquery', ['google-cloud-bigquery', 'google-cloud-storage']),
    ('import google.auth\nfrom google.oauth2 import service_account', ['google-auth']),
    ('from google.protobuf import timestamp_pb2', ['protobuf']),
    ('import google.api_core.exceptions', ['google-api-core']),
    ('import googleapiclient.discovery', ['google-api-python-client']),
    ('import google\nimport google.cloud', ['google-api-python-client']),
    ('from google.cloud import', []),
])
def test_google_namespace_packages(code, expected):
    assert dependencies(code, 'python') == expected


def test_pins_and_esm():
    assert pinned(['requests'], ['requests==2.32.3'], 'python') == ['requests==2.32.3']
    assert pinned(['@a/b'], ['@a/b@1.2.3'], 'javascript') == ['@a/b@1.2.3']
    assert is_esm('await Promise.resolve(1)')
    assert not is_esm("const fs = require('fs')")


def test_python_pin_with_extras():
    assert pinned(['example-sdk', 'other'], ['Example.SDK[async,socks]==1.2.3', 'unused==1.0'], 'python') == [
        'Example.SDK[async,socks]==1.2.3', 'other',
    ]


@pytest.mark.parametrize('code,esm', [
    ("const fs = require('node:fs'); async function main() { await Promise.resolve(); } main();", False),
    ("const fs = require('node:fs'); const main = async () => { await Promise.resolve(); }; main();", False),
    ("const main = async () => await Promise.resolve(); main();", True),
    ("const fs = require('node:fs'); await Promise.resolve();", False),
    ("if (true) { await Promise.resolve(); }", True),
    ("// import x from 'x'\nconsole.log('await import export');", True),
    ("import('node:fs').then(() => {});", True),
    ("import fs from 'node:fs'; await Promise.resolve();", True),
    ("module.exports = {};", False),
    ("exports.value = 1;", False),
    ("console.log(__dirname);", False),
    ("console.log(__filename);", False),
    ("import{createRequire}from'node:module'; const require = createRequire(import.meta.url); require('node:fs');", True),
    ("import*as m from'node:module'; const require = m.createRequire(import.meta.url); require('node:fs');", True),
    ('', True),
])
def test_javascript_module_mode(code, esm):
    assert is_esm(code) is esm


@pytest.mark.parametrize('runner', ['local', pytest.param('docker', marks=[pytest.mark.docker, pytest.mark.skipif(not __import__('os').environ.get('SENTINEL_DOCKER_TESTS'), reason='Set SENTINEL_DOCKER_TESTS=1')])])
@pytest.mark.parametrize('code,expected', [
    ("const fs = require('node:fs'); async function main() { await Promise.resolve(); } main();", 'pass'),
    ("const fs = require('node:fs'); await Promise.resolve();", 'pass'),
    ("const fs = require('node:fs'); await Promise.reject(new Error('broken'));", 'runtime_error'),
    ("console.log(`${await Promise.resolve('value')}`);", 'pass'),
    ("const f = async () => await Promise.resolve(1)\nawait f()", 'pass'),
    ("const fs = require('node:fs'); console.log(`${await Promise.resolve('value')}`);", 'syntax_error'),
    ("const fs = require('node:fs')\nconst f = async () => await Promise.resolve(1)\nawait f()", 'pass'),
    ("const fs = require('node:fs'); const broken = ;", 'syntax_error'),
    ("module.exports = {value: 1};", 'pass'),
    ("exports.value = 1;", 'pass'),
    ("console.log(__dirname);", 'pass'),
    ("console.log(__filename);", 'pass'),
    ("const fs = require('node:fs'); for await (const x of [1, 2]) { console.log(x); }", 'pass'),
    ("import{createRequire}from'node:module'; const require = createRequire(import.meta.url); require('node:fs');", 'pass'),
    ("import*as m from'node:module'; const require = m.createRequire(import.meta.url); require('node:fs');", 'pass'),
])
def test_async_commonjs_execution(code, expected, runner):
    if runner == 'local' and not shutil.which('node'):
        pytest.skip('Local Node required')
    result = run(Snippet('js', 'a.md', 1, 'javascript', code), runner)
    assert result['status'] == expected, result['stderr']


@pytest.mark.parametrize('message,code,expected', [('', 0, 'pass'), ('SyntaxError: x', 1, 'syntax_error'),
    ('Cannot find module x', 1, 'missing_dependency'), ('401 Unauthorized', 1, 'needs_credentials'),
    ("KeyError: 'API_KEY'", 1, 'needs_credentials'), ('process.env.API_KEY undefined', 1, 'needs_credentials'),
    ('RuntimeError', 1, 'runtime_error'), ('', 1, 'runtime_error')])
def test_classifier(message, code, expected):
    assert classify(message, code) == expected


@pytest.mark.parametrize('stderr,exit_code,code,expected', [
    ("StripeAuthenticationError: Invalid API Key provided: sk_test_...\n  statusCode: 401", 0, '', 'needs_credentials'),
    ('{ statusCode: 200 }', 0, '', 'pass'),
    ("SyntaxError: The requested module 'https-proxy-agent' does not provide an export named 'default'", 1, '', 'runtime_error'),
    ('OpenAIError: Missing credentials. Please pass an `apiKey`', 1, '', 'needs_credentials'),
    ("Error: Set OPENAI_MODEL to a model", 1, "const model = process.env['OPENAI_MODEL'];", 'needs_credentials'),
    ("Error: Set OPENAI_MODEL to a model", 1, "const model = 'x';", 'runtime_error'),
    ('ReferenceError: stripeClient is not defined', 1, 'stripeClient.customers.list();', 'needs_context'),
    ('ReferenceError: document is not defined', 1, "document.querySelector('a');", 'needs_context'),
    ('ReferenceError: client is not defined', 1, 'if (x) { const client = 1; }\nclient.go();', 'runtime_error'),
    ('ReferenceError: Stripe is not defined', 1, "import { Stripe } from 'stripe';", 'runtime_error'),
    ('ReferenceError: a is not defined', 1, 'const { a, b } = c;', 'runtime_error'),
    ('ReferenceError: err is not defined', 1, 'try { x(); } catch (err) {}\nconsole.log(err);', 'runtime_error'),
    ('ReferenceError: item is not defined', 1, 'list.forEach((item) => 1);\nitem.go();', 'runtime_error'),
    ('ReferenceError: client is not defined', 1, '// const client = new Client();\nclient.request();', 'needs_context'),
    ('ReferenceError: client is not defined', 1, '/* const client = new Client(); */\nclient.request();', 'needs_context'),
    ('ReferenceError: client is not defined', 1, "const url = 'https://x'; const client = 1;\n{ }\nclient.go();", 'runtime_error'),
    ('ReferenceError: client is not defined', 1, "log('const client = 1');\nclient.go();", 'needs_context'),
])
def test_javascript_verdicts(stderr, exit_code, code, expected):
    assert classify(stderr, exit_code, code=code) == expected


def test_needs_context_reason_and_leaderboard(tmp_path):
    result = run(Snippet('js', 'a.md', 1, 'javascript', 'stripeClient.customers.list();'), 'local')
    if shutil.which('node'):
        assert result['status'] == 'needs_context'
        assert '`stripeClient`' in result['reason']
    store = Store(tmp_path / 'board.db')
    rid = store.start('repo')
    store.finish(rid, [{'id': 'a', 'status': 'needs_context', 'versions': {}}, {'id': 'b', 'status': 'runtime_error', 'versions': {}}], {})
    assert store.leaderboard()[0]['runnable'] == 1 and store.leaderboard()[0]['percent'] == 100


def test_timeout_install():
    assert classify('', -9, timed_out=True) == 'timeout'
    assert classify('', 1, install_failed=True) == 'missing_dependency'


@pytest.mark.parametrize('output,expected', [
    ('Traceback (most recent call last):\n    print(os.environ.get("CONFIG"))\nTypeError: None is not iterable',
     'runtime_error'),
    ('Traceback (most recent call last):\n    print(401 / 0)\nZeroDivisionError: division by zero',
     'runtime_error'),
    ('ModuleNotFoundError: No module named "plugin"\n\n'
     'The above exception was the direct cause of the following exception:\n'
     'demo.SDKError: Missing credentials. Please pass an api_key.', 'needs_credentials'),
    ("Traceback (most recent call last):\nKeyError: 'CLIENT_CERTIFICATE_CHAIN'", 'needs_credentials'),
    ("Traceback (most recent call last):\nKeyError: 'CONFIG_FILE'", 'runtime_error'),
])
def test_classifier_uses_final_error(output, expected):
    assert classify(output, 1) == expected


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


@pytest.mark.parametrize('failure', ['daemon unavailable', 'image pull failed', 'creation timed out', 'executable missing'])
def test_docker_infrastructure_error(tmp_path, monkeypatch, failure):
    def fail(*args, **kwargs):
        if failure == 'executable missing':
            raise FileNotFoundError('docker')
        return failure, 1, failure == 'creation timed out'

    monkeypatch.setattr('snippet_sentinel.runner.command', fail)
    (tmp_path / 'a.md').write_text('```py\nprint(1)\n```')
    db = tmp_path / 'scan.db'
    with pytest.raises(InfrastructureError, match='Docker'):
        scan(str(tmp_path), db=db)
    stored = Store(db).get(1)
    assert stored['finished'] and 'Docker' in stored['error']
    assert stored['counts'] == {} and stored['results'] == []
    assert Store(db).leaderboard()[0]['broken'] == 0
    client = TestClient(create_app(db))
    assert 'Docker' in client.get('/runs/1').text


def test_cli_infrastructure_error(tmp_path, monkeypatch, capsys):
    from snippet_sentinel.cli import main

    def fail(*args, **kwargs):
        raise InfrastructureError('Docker daemon unavailable')

    monkeypatch.setattr('snippet_sentinel.cli.scan', fail)
    monkeypatch.setattr('sys.argv', ['snippet-sentinel', 'scan', str(tmp_path)])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1
    assert 'Docker daemon unavailable' in capsys.readouterr().err


@pytest.mark.parametrize('url', ['', 'not a URL', 'https://example.com/a', '\"><script>alert(1)</script>'])
def test_invalid_scan_form_retains_value(tmp_path, url):
    db = tmp_path / 'scan.db'
    client = TestClient(create_app(db))
    response = client.post('/scan', data={'url': url})
    assert response.status_code == 400
    assert response.headers['content-type'].startswith('text/html')
    assert 'role="alert"' in response.text
    assert 'autofocus aria-invalid="true" aria-describedby="scan-error"' in response.text
    assert 'HTTPS GitHub repository URL' in response.text
    value = re.search(r'name="url" value="([^"]*)"', response.text).group(1)
    assert unescape(value) == url
    assert '<script>' not in response.text
    assert Store(db).leaderboard() == []


@pytest.mark.parametrize('filename', ['docs/sample.md', 'sample.mdx', 'README.MD', 'guide.markdown'])
@pytest.mark.parametrize('static', [False, True])
def test_github_markdown_source_links(filename, static):
    snippet = {**Snippet('x', filename, 42, 'python', '1').dict(),
               'status': 'pass', 'runner': 'docker', 'versions': {}, 'stderr': '', 'exit_code': 0}
    page = render_run({'id': 1, 'repo': 'https://github.com/a/b', 'ref': 'abc123',
                       'finished': True, 'started': '', 'counts': {}, 'stats': {}, 'results': [snippet]}, static=static)
    assert f'https://github.com/a/b/blob/abc123/{filename}?plain=1#L42' in page


def test_indented_fence(tmp_path):
    (tmp_path / 'a.md').write_text('  ```python\n  print(1)\n  ```\n')
    assert extract(tmp_path)[0][0].code == 'print(1)'


def test_extract_non_runnable_python_blocks(tmp_path):
    (tmp_path / 'a.md').write_text(
        '\x60\x60\x60python\n$ pip install sdk\n\x60\x60\x60\n'
        '\x60\x60\x60py\n# Install the SDK\npython -m pip install sdk\nexport SDK_MODE=demo\n\x60\x60\x60\n'
        '\x60\x60\x60pycon\n>>> import json\n\x60\x60\x60\n'
        '\x60\x60\x60python {lint="skip"}\nprint(1)\n\x60\x60\x60\n'
        '\x60\x60\x60python {test="skip" lint="skip"}\nprint(2)\n\x60\x60\x60\n'
        '\x60\x60\x60python\ntext = """\n...\n>>> ignored\n$ pip install sdk\n"""\nprint(text)\n\x60\x60\x60\n'
        '\x60\x60\x60python\npip = 1\nprint(pip)\n\x60\x60\x60\n'
    )
    snippets, _ = extract(tmp_path)
    assert [s.reason for s in snippets] == [
        'shell commands in a Python fence', 'shell commands in a Python fence', 'REPL transcript', '',
        'info-string opt-out', '', '',
    ]


@pytest.mark.parametrize('body,output,expected', [
    ('json.dumps(1)', '', 'skipped'),
    ('await json.load(None)', '', 'skipped'),
    ('jsons.dumps(1)', '', 'runtime_error'),
    ('await unknown()', '', 'syntax_error'),
    ('raise ValueError("expected")', 'ValueError: expected', 'skipped'),
    ('raise TypeError("wrong exception")', 'ValueError: expected', 'runtime_error'),
    ('print("healthy result")', 'ValueError: illustrative text', 'pass'),
])
def test_run_documentation_fragments(tmp_path, body, output, expected):
    text = '\x60\x60\x60pycon\n>>> import json\n\x60\x60\x60\n\n\x60\x60\x60python\n' + body + '\n\x60\x60\x60\n'
    if output:
        text += '\n\x60\x60\x60output\n' + output + '\n\x60\x60\x60\n'
    (tmp_path / 'a.md').write_text(text)
    snippet = extract(tmp_path)[0][1]
    result = run(snippet, runner='local')
    assert result['status'] == expected, result
    assert bool(result['reason']) == (expected == 'skipped')


def test_leaderboard_places_unassessed_runs_after_measured_runs(tmp_path):
    db = tmp_path / 'leaderboard.db'
    store = Store(db)
    for repo, statuses, error in [
        ('measured', ['pass'], ''),
        ('credentials', ['needs_credentials', 'skipped'], ''),
        ('failed', [], 'Docker unavailable'),
        ('running', None, ''),
    ]:
        rid = store.start(repo)
        if statuses is not None:
            store.finish(rid, [{'id': str(i), 'status': status} for i, status in enumerate(statuses)], {}, error=error)
    assert [(r['repo'], r['percent']) for r in store.leaderboard()] == [
        ('measured', 0), ('credentials', None), ('failed', None), ('running', None),
    ]
    html = TestClient(create_app(db)).get('/').text
    assert html.count('0.0%') == 1
    assert '0 of 1 checked' in html
    assert 'No eligible snippets' in html
    assert html.count('Not available') == 2
    assert html.count('class="rank"') == 1


def test_identity_includes_normalized_language(tmp_path):
    path = tmp_path / 'a.md'
    path.write_text('```py\n1\n```\n```js\n1\n```\n```python\n1\n```')
    snippets, _ = extract(tmp_path)
    assert snippets[0].id != snippets[1].id
    assert snippets[0].id == snippets[2].id
    identity = snippets[0].id
    path.write_text('\n\n```python\n1\n```')
    assert extract(tmp_path)[0][0].id == identity


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
