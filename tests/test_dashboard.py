"""Presentation regressions: scores, source navigation, and exported reports."""
import re

import pytest
from fastapi.testclient import TestClient

from snippet_sentinel.extractor import Snippet
from snippet_sentinel.storage import Store
from snippet_sentinel.web import create_app, present_run, readable_date, render_run


def result(status, filename='docs/guide with spaces.md'):
    return {**Snippet(status, filename, 42, 'python', 'print(1)').dict(),
            'status': status, 'runner': 'docker', 'versions': {}, 'stderr': '', 'exit_code': 0}


def test_readable_dates_and_repo_names():
    assert readable_date('2026-11-19T03:07:00+01:00') == 'Nov 19, 2026 · 02:07 UTC'
    assert readable_date('2026-11-19T02:07:00Z') == 'Nov 19, 2026 · 02:07 UTC'
    assert readable_date('') == readable_date('invalid') == 'Not recorded'
    for repo, name in [('/private/worker/demo-docs', 'demo-docs'),
                       ('C:\\worker\\demo-docs\\', 'demo-docs'),
                       ('https://github.com/a/b.git', 'a/b')]:
        assert present_run({'repo': repo, 'counts': {}})['repo_name'] == name


def test_leaderboard_scores_exclusions_and_local_privacy(tmp_path):
    db = tmp_path / 'runs.db'
    store = Store(db)
    rid = store.start('/private/worker/demo-docs')
    store.finish(rid, [result(s) for s in ['pass', 'runtime_error', 'skipped', 'needs_credentials']], {})
    page = TestClient(create_app(db)).get('/').text
    assert '/private/worker' not in page
    assert 'demo-docs' in page and 'Local folder' in page
    assert '50.0%' in page and '1 of 2 checked' in page
    assert '1 need credentials · 1 skipped' in page
    assert page.index('<table') < page.index('<summary>Scan a GitHub')
    assert re.search(r'<time datetime="[^"]+">\w+ \d+, \d+ · \d+:\d+ UTC</time>', page)


@pytest.mark.parametrize('state,expected', [('excluded', 'Not evaluated'),
                                         ('empty', 'No eligible snippets'),
                                         ('pending', 'Scanning…'), ('failed', 'Scan failed')])
def test_unavailable_scores_are_not_zero_percent(tmp_path, state, expected):
    db = tmp_path / 'runs.db'
    store = Store(db)
    rid = store.start('https://github.com/a/b')
    if state != 'pending':
        store.finish(rid, [result('skipped')] if state == 'excluded' else [], {},
                     error='Docker unavailable' if state == 'failed' else '')
    client = TestClient(create_app(db))
    assert expected in client.get('/').text
    assert '0.0%' not in client.get('/').text
    assert '0.0%' not in client.get(f'/runs/{rid}').text


def test_verdict_filter_source_and_static_export(tmp_path):
    db = tmp_path / 'runs.db'
    store = Store(db)
    rid = store.start('https://github.com/a/b')
    store.finish(rid, [result('pass'), result('needs_credentials'), result('syntax_error')], {}, 'abc123')
    client = TestClient(create_app(db))
    page = client.get(f'/runs/{rid}').text
    assert page.index('Broken · Syntax error') < page.index('>Passed</span>')
    assert 'https://github.com/a/b/blob/abc123/docs/guide%20with%20spaces.md?plain=1#L42' in page
    assert 'View source on GitHub' in page and 'line 42' in page
    assert 'Credentials needed' in page and 'detected heuristically' in page
    filtered = client.get(f'/runs/{rid}?status=broken').text
    assert filtered.count('<article ') == 1
    assert 'Broken · Syntax error' in filtered and 'Clear filter' in filtered
    filtered = client.get(f'/runs/{rid}?status=needs_credentials').text
    assert filtered.count('<article ') == 1
    assert 'Likely requires a key' in filtered
    exported = render_run(store.get(rid), static=True)
    assert 'View source on GitHub' in exported
    assert 'href="/runs/' not in exported and 'href="/"' not in exported
    assert '<form' not in exported


def test_local_sources_and_untrusted_text_are_not_links(tmp_path):
    store = Store(tmp_path / 'runs.db')
    rid = store.start('/private/worker/<script>alert(1)</script>')
    store.finish(rid, [result('syntax_error', 'docs/<script>.md')], {}, 'abc123')
    page = render_run(store.get(rid))
    assert '<script>' not in page
    assert 'Local file · open this path at line 42' in page
    assert 'View source on GitHub' not in page
    assert '/private/worker' not in page
