import os
import re
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

from .extractor import extract
from .runner import run
from .storage import Store

DEFAULT_MAX_SNIPPETS = 500


def redact_git_error(value):
    if isinstance(value, bytes):
        value = value.decode(errors='replace')
    value = re.sub(r'\b[a-z][a-z0-9+.-]*://[^\s\'"<>]+', '[redacted URL]', value or '', flags=re.I)
    value = re.sub(r'\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b', '[redacted]', value)
    value = re.sub(r'(?im)\b(?:proxy-)?authorization\s*[:=][^\r\n]*', 'Authorization: [redacted]', value)
    value = re.sub(r'''(?i)\b(?:[\w-]*token|password|passwd|secret|api[_-]?key)\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s,;]+)''', '[redacted credential]', value)
    return value.strip()[-4000:]


def git_error(stderr, ref=None):
    detail = redact_git_error(stderr)
    lower = detail.lower()
    if ref and any(text in lower for text in ('remote branch', "couldn't find remote ref", 'not a valid branch')):
        message = f"Branch or tag '{redact_git_error(ref)}' not found"
    elif re.search(r'repository .*not found', lower) or any(text in lower for text in ('repository not found', 'remote: not found', 'authentication failed', 'could not read username',
                                        'could not read password', 'terminal prompts disabled', 'access denied',
                                        'permission denied', 'error: 404', 'error: 403')):
        message = "Repository not found or not accessible — private repos aren't supported / check the URL"
    elif any(text in lower for text in ('could not resolve', 'failed to connect', 'network is unreachable',
                                        'connection timed out', 'connection refused', 'connection reset')):
        message = 'Could not reach GitHub (network/DNS failure). Check your connection and try again'
    else:
        message = 'Git clone failed'
    return message + (f'\nGit: {detail}' if detail else ' (git returned no error details)')


def github_url(value):
    parsed = urlparse(value)
    parts = parsed.path.strip('/').removesuffix('.git').split('/')
    if parsed.scheme != 'https' or parsed.netloc != 'github.com' or len(parts) != 2 or not all(parts):
        raise ValueError('Use an HTTPS GitHub repository URL: https://github.com/owner/repo')
    if any(not all(c.isalnum() or c in '._-' for c in p) or p.startswith('-') for p in parts):
        raise ValueError('Invalid GitHub repository name')
    return 'https://github.com/' + '/'.join(parts)


def scan(source, db='sentinel.db', ref=None, lang=None, runner='docker', jobs=4, timeout=60, pins=(), run_id=None,
         max_snippets=DEFAULT_MAX_SNIPPETS, progress=None):
    if max_snippets < 0:
        raise ValueError('max_snippets must be nonnegative (0 means unlimited)')
    progress = progress or (lambda message: None)
    store = Store(db)
    remote = github_url(source) if '://' in source else None
    repo = remote or str(Path(source).resolve())
    run_id = run_id or store.start(repo, ref or '')
    store.set_run_settings(run_id, pins, timeout)
    sha = ''
    try:
        with tempfile.TemporaryDirectory(prefix='sentinel-repo-') as temp:
            if remote:
                progress('Cloning repository…')
                args = ['git', 'clone', '--depth', '1']
                if ref:
                    args += ['--branch', ref]
                try:
                    subprocess.run([*args, '--', remote, temp], check=True, capture_output=True, timeout=120,
                                   env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'})
                except subprocess.CalledProcessError as exc:
                    raise ValueError(git_error(exc.stderr, ref)) from None
                except subprocess.TimeoutExpired:
                    raise ValueError('Git clone timed out after 120 seconds. Check your network connection and repository size') from None
                root = Path(temp)
            else:
                root = Path(source).resolve()
                if not root.is_dir():
                    if source.lower().startswith(('github.com/', 'www.github.com/', 'git@github.com:')):
                        raise ValueError('Use an HTTPS GitHub repository URL: https://github.com/owner/repo')
                    raise ValueError('Local repository directory does not exist')
                if ref:
                    raise ValueError('--ref applies to GitHub URLs; check out the desired local ref first')
            commit = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'], capture_output=True, text=True)
            sha = commit.stdout.strip() if commit.returncode == 0 else ''
            snippets, stats = extract(root)
            if lang:
                snippets = [s for s in snippets if s.lang == lang]
            found = len(snippets)
            progress(f'Found {found} snippets in {len({s.file for s in snippets})} files')
            if max_snippets:
                snippets = snippets[:max_snippets]
            total = len(snippets)
            stats.update(found=found, selected=total, truncated=found - total, max_snippets=max_snippets)
            if found > total:
                progress(f'Snippet limit reached: scanning first {total} of {found}; {found - total} omitted. Use --max-snippets 0 for unlimited')
            progress(f'0/{total} done')
            results = [None] * total
            last_progress = time.monotonic()
            with ThreadPoolExecutor(max_workers=jobs) as pool:
                futures = {pool.submit(run, s, runner, timeout, pins): i for i, s in enumerate(snippets)}
                for done, future in enumerate(as_completed(futures), 1):
                    results[futures[future]] = future.result()
                    if done in (1, total) or time.monotonic() - last_progress >= 1:
                        progress(f'{done}/{total} done')
                        last_progress = time.monotonic()
            store.finish(run_id, results, stats, sha)
    except Exception as exc:
        store.finish(run_id, [], {}, sha, str(exc))
        raise
    return store.get(run_id)
