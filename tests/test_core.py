import re
import shutil
from html import unescape
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from snippet_sentinel.classifier import classify
from snippet_sentinel.dependencies import script_name, dependencies, is_esm, pinned
from snippet_sentinel.extractor import extract, javascript_fragment
from snippet_sentinel.extractor import Snippet
from snippet_sentinel.runner import InfrastructureError, documented_failure, run
from snippet_sentinel.scan import github_url, scan
from snippet_sentinel.storage import Store
from snippet_sentinel.web import create_app, render_run


def test_extract(tmp_path):
    (tmp_path / 'a.mdx').write_text('  ~~~py\nprint(1)\n  ~~~\n```ts\nx\n```\n<!-- sentinel:skip -->\n```js\nx\n```\n```python norun\nx\n```\n```py\n>>> x\n```\n```js\n// ...\n```\n')
    (tmp_path / 'vendor').mkdir()
    (tmp_path / 'vendor/x.md').write_text('```py\nx\n```')
    snippets, stats = extract(tmp_path)
    assert len(snippets) == 6
    assert snippets[0].line == 2 and snippets[0].lang == 'python'
    assert snippets[1].lang == 'typescript' and not snippets[1].reason
    assert all(s.reason for s in snippets[2:])
    assert stats == {'unsupported': 0}
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
@pytest.mark.parametrize('lang,code,expected', [
    ('typescript', "const value: number = 1;\nconsole.log(value);", 'pass'),
    ('typescript', "enum Color { Red }\ninterface P { c: Color }\nconst p: P = { c: Color.Red };\nawait Promise.resolve(p);", 'pass'),
    ('typescript', "import { strict as assert } from 'node:assert';\nimport type { Stats } from 'node:fs';\nimport { Dirent } from 'node:fs';\nlet d: Dirent | undefined;\nassert.equal(d, undefined);", 'pass'),
    ('typescript', "const fs = require('node:fs');\nconst n: number = fs.constants.F_OK;", 'pass'),
    ('typescript', "import type { Stats } from 'node:fs';\nconst fs = require('node:fs');\nlet s: Stats | undefined;", 'pass'),
    ('typescript', "import fs = require('node:fs');\nconst n: number = fs.constants.F_OK;", 'pass'),
    ('typescript', "const fs = require('node:fs');\nconst n: number = await Promise.resolve(fs.constants.F_OK);", 'pass'),
    ('typescript', "const fs = require('node:fs');\nawait Promise.resolve();\nimport('node:path').then((path) => path.join(fs.constants.F_OK + ''));", 'pass'),
    ('typescript', "const value: number = ;", 'syntax_error'),
    ('typescript', "const p = (x: number) => x;\np(1).toFixed(2).missing();", 'runtime_error'),
    ('javascript', "const React = { createElement: () => ({}) };\nconsole.log(<b>hi</b>, <br />);", 'pass'),
])
def test_typescript_and_jsx_execution(lang, code, expected, runner):
    if runner == 'local' and not shutil.which('node'):
        pytest.skip('Local Node required')
    result = run(Snippet('ts', 'a.md', 1, lang, code), runner)
    assert result['status'] == expected, result['stderr']
    assert 'tsx' not in result['versions']


@pytest.mark.parametrize('code,lang,expected', [
    ('const x: number = 1;', 'typescript', 'snippet.ts'),
    ("import x from 'y';\nconst el = <App />;", 'typescript', 'snippet.tsx'),
    ('const el = <p>hi</p>;', 'javascript', 'snippet.jsx'),
    ("const ok = 1 < 2 && 3 > 2;", 'javascript', 'snippet.mjs'),
    ('const el = <><b>a</b></>;', 'typescript', 'snippet.tsx'),
    ('const el = <Button onClick={() => go()} />;', 'javascript', 'snippet.jsx'),
    ("const html = '<p>hi</p>';\nconst n = <number>JSON.parse('1');", 'typescript', 'snippet.ts'),
    ("import x from 'y';", 'javascript', 'snippet.mjs'),
])
def test_script_name(code, lang, expected):
    assert script_name(code, lang) == expected


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
    ('ReferenceError: run is not defined', 1, "for await (const [content, snapshot] of run.events('x')) {}", 'needs_context'),
    ('ReferenceError: require is not defined in ES module scope, you can use import instead', 1, "require('x');", 'runtime_error'),
    ("Error [ERR_MODULE_NOT_FOUND]: Cannot find module '/components/EmailTemplate' imported from /work/snippet.jsx", 1, '', 'needs_context'),
    ("Error [ERR_MODULE_NOT_FOUND]: Cannot find module './util.js' imported from /tmp/x/snippet.mjs", 1, '', 'needs_context'),
    ("Error [ERR_MODULE_NOT_FOUND]: Cannot find package 'ws' imported from /work/node_modules/openai/ws.mjs", 1, '', 'missing_dependency'),
    ("Error [ERR_MODULE_NOT_FOUND]: Cannot find module '/work/node_modules/openai/missing' imported from /work/snippet.mjs", 1, '', 'missing_dependency'),
    ("StripeAPIError: Invalid JSON received from the Stripe API\n  SyntaxError: Unexpected token '<', \"<!doctype \"... is not valid JSON", 1, '', 'runtime_error'),
    ("Error: Missing Azure OpenAI configuration", 1, "const e = process.env['AZURE_OPENAI_ENDPOINT'];\nif (!e) throw new Error('Missing Azure OpenAI configuration');", 'needs_credentials'),
    ("Error: Missing Azure OpenAI configuration", 1, "throw new Error('Missing Azure OpenAI configuration');", 'runtime_error'),
    ('OpenAIError: Must provide one of the `baseURL` or `endpoint` arguments, or the `AZURE_OPENAI_ENDPOINT` environment variable', 1, '', 'needs_credentials'),
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
        assert run['counts'] == {'pass': 3, 'syntax_error': 2, 'missing_dependency': 2, 'runtime_error': 2, 'needs_credentials': 2, 'skipped': 5}
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


def test_unclosed_fence_at_eof(tmp_path):
    (tmp_path / 'a.md').write_text('\x60\x60\x60python\nprint(1)\n')
    snippets, _ = extract(tmp_path)
    assert len(snippets) == 1
    assert snippets[0].code == 'print(1)'
    assert snippets[0].expected_error == ''


def test_commented_python_repl_transcript(tmp_path):
    (tmp_path / 'a.md').write_text('\x60\x60\x60python\n# Interactive example\n\n>>> print(1)\n1\n\x60\x60\x60\n')
    snippets, _ = extract(tmp_path)
    assert snippets[0].reason == 'REPL transcript'


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
    ('try:\n    raise ValueError("expected")\nexcept ValueError as exc:\n    raise TypeError from exc',
     'ValueError: expected', 'runtime_error'),
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


def test_name_error_with_import_suggestion():
    output = "NameError: name 'json' is not defined. Did you forget to import 'json'?"
    snippet = Snippet('x', 'a.md', 1, 'python', 'json.dumps(1)', context_names=('json',))
    assert documented_failure(snippet, 'runtime_error', output) == 'requires earlier setup: json'
    snippet.context_names = ('other',)
    assert documented_failure(snippet, 'runtime_error', output) == ''


@pytest.mark.parametrize('markdown,expected,reason', [
    ('~~~py\njson.dumps(value)\n~~~\n~~~py\nimport json\n~~~', 'skipped', 'requires example setup: json'),
    ('Compare `json.dumps` with the alternative.\n~~~py\njson.dumps(value)\n~~~',
     'skipped', 'requires example setup: json'),
    ('Show the equivalent call.\n~~~py\njson.dumps(value)\n~~~\n\n## API\nUse `json.dumps` to serialize.',
     'skipped', 'requires example setup: json'),
    ('~~~py\nimport json\n~~~\n\nUse `io.BytesIO` for a file-like object.\n'
     '~~~py\nimport io\nstream = io.BytesIO(contents)\njson.load(stream)\n~~~',
     'skipped', 'requires example setup: contents'),
    ('~~~py\nopen("/path/to/config.json")\n~~~', 'skipped', 'placeholder path'),
    ('~~~py\nopen("/missing/config.json")\n~~~', 'runtime_error', ''),
    ('~~~py\nprint("/path/to/config.json"); open("/missing/config.json")\n~~~', 'runtime_error', ''),
    ('~~~py\nimport os\nos.rename("/path/to/source -> copy", "destination")\n~~~',
     'skipped', 'placeholder path: /path/to/source -> copy'),
    ('~~~py\nimport os\nopen("source", "w").close()\nos.rename("source", "/path/to/destination")\n~~~',
     'skipped', 'placeholder path: /path/to/destination'),
    ('~~~py\nimport os\nos.rename("missing", "destination"); print("/path/to/config.json")\n~~~',
     'runtime_error', ''),
    ('~~~py\nimport ssl\nssl.create_default_context(cafile="/path/to/ca.pem")\n~~~',
     'skipped', 'placeholder path'),
    ('~~~py\nimport ssl\nssl.create_default_context(cafile="/missing/cert.pem"); print("/path/to/config.json")\n~~~',
     'runtime_error', ''),
    ('Compare `json.dumps`.\n~~~py\njsno.dumps(value)\n~~~', 'runtime_error', ''),
    ('Compare `json.dumps`.\n~~~py\njson.removed_api(value)\n~~~', 'runtime_error', ''),
    ('Compare `json.dumps`.\n~~~py\njson.dumps(value)\nimport json\n~~~', 'runtime_error', ''),
    ('Compare `json.dumps`.\n~~~py\nvalue = 1\nprint(json.dumps(value))\n~~~\n~~~py\nimport json\n~~~',
     'runtime_error', ''),
    ('Compare `json.dumps`.\n~~~py\nimport json\njson.removed_api(value)\n~~~', 'runtime_error', ''),
    ('Compare `json.dumps`.\n~~~py\nfrom json import removed_api\n~~~', 'runtime_error', ''),
])
def test_illustrative_python_verdicts(tmp_path, markdown, expected, reason):
    (tmp_path / 'a.md').write_text(markdown)
    snippets, _ = extract(tmp_path)
    snippet = next(s for s in snippets if s.code != 'import json')
    result = run(snippet, runner='local')
    assert result['status'] == expected, result
    assert reason in result['reason'] if reason else not result['reason']


@pytest.mark.parametrize('body,unavailable,expected', [
    ('import sync_helper as helper\nhelper.get_token(...)', 'sync_helper', 'skipped'),
    ('import sync_helper as helper\nhelper.get_token("production")', 'sync_helper', 'missing_dependency'),
    ('import sync_helper as helper\nhelper.get_token(...)', 'sync_helper==99.0', 'missing_dependency'),
    ('import sync_helper as helper\nimport unrelated\nhelper.get_token(...)', 'unrelated', 'missing_dependency'),
])
def test_run_illustrative_dependency(monkeypatch, body, unavailable, expected):
    def command(args, *_):
        if 'venv' in args:
            return '', 0, False
        return f'SENTINEL_INSTALL_FAILED\nERROR: No matching distribution found for {unavailable}\n', 1, False

    monkeypatch.setattr('snippet_sentinel.runner.command', command)
    result = run(Snippet('x', 'a.md', 1, 'python', body), runner='local')
    assert result['status'] == expected, result
    assert bool(result['reason']) == (expected == 'skipped')


def test_run_documented_javascript_reference_error(tmp_path):
    if not shutil.which('node') or not shutil.which('npm'):
        pytest.skip('Local Node and npm required')
    (tmp_path / 'a.md').write_text(
        '\x60\x60\x60js\nconsole.log(exampleValue)\n\x60\x60\x60\n\n'
        '\x60\x60\x60output\nReferenceError: exampleValue is not defined\n\x60\x60\x60\n'
    )
    snippet = extract(tmp_path)[0][0]
    result = run(snippet, runner='local')
    assert result['status'] == 'skipped', result
    assert result['reason'] == 'demonstrates ReferenceError as documented'


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


@pytest.mark.parametrize('code,expected', [
    ("// Before\nconst c = Stripe('k');\n\n// After\nconst c = new Stripe('k');", 'before/after comparison'),
    ("import Stripe from 'npm:stripe';", 'Deno-only import'),
    ("import Stripe from 'stripe'; // Deno: import Stripe from 'npm:stripe';\nconst u = 'https://x';", ''),
    ("// \u274c Before (v22.0.0)\nconst t = {};\n\n// \u2705 After (v22.1.2+)\nconst t = {};", 'before/after comparison'),
    (".on('event', (event: AssistantStreamEvent) => ...)", 'method-chain fragment'),
    ("await .finalMessages() : Promise<Message[]>", 'method-chain fragment'),
    ("await resend.emails.send({ ... });", 'placeholder ellipsis'),
    ("messages: [{ role: 'user', content: '\u2026' }],\nrun(\u2026);", 'placeholder ellipsis'),
    ("const all = [...items, ...more];\nf(...args);", ''),
    ("const m = 'abc'.match(/(...)/);\nconst n = /[...]/.test('.');", ''),
    ("console.log('Loading\u2026 ...)');", ''),
    ("const url = 'https://example.com'; // before and after", ''),
    ("client.chat.completions\n  .create({ model: 'x' });", ''),
])
def test_javascript_fragment(code, expected):
    assert javascript_fragment(code) == expected
