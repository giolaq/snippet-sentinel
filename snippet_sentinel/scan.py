import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

from .extractor import extract
from .runner import run
from .storage import Store


def github_url(value):
    parsed = urlparse(value)
    parts = parsed.path.strip('/').removesuffix('.git').split('/')
    if parsed.scheme != 'https' or parsed.netloc != 'github.com' or len(parts) != 2 or not all(parts):
        raise ValueError('Use an HTTPS GitHub repository URL: https://github.com/owner/repo')
    if any(not all(c.isalnum() or c in '._-' for c in p) or p.startswith('-') for p in parts):
        raise ValueError('Invalid GitHub repository name')
    return 'https://github.com/' + '/'.join(parts)


def scan(source, db='sentinel.db', ref=None, lang=None, runner='docker', jobs=4, timeout=60, pins=(), run_id=None):
    store = Store(db)
    remote = github_url(source) if '://' in source else None
    repo = remote or str(Path(source).resolve())
    run_id = run_id or store.start(repo, ref or '')
    store.set_run_settings(run_id, pins, timeout)
    sha = ''
    try:
        with tempfile.TemporaryDirectory(prefix='sentinel-repo-') as temp:
            if remote:
                args = ['git', 'clone', '--depth', '1']
                if ref:
                    args += ['--branch', ref]
                subprocess.run([*args, '--', remote, temp], check=True, capture_output=True, timeout=120)
                root = Path(temp)
            else:
                root = Path(source).resolve()
                if not root.is_dir():
                    raise ValueError('Local repository directory does not exist')
                if ref:
                    raise ValueError('--ref applies to GitHub URLs; check out the desired local ref first')
            commit = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'], capture_output=True, text=True)
            sha = commit.stdout.strip() if commit.returncode == 0 else ''
            snippets, stats = extract(root)
            if lang:
                snippets = [s for s in snippets if s.lang == lang]
            with ThreadPoolExecutor(max_workers=jobs) as pool:
                results = list(pool.map(lambda s: run(s, runner, timeout, pins), snippets))
            store.finish(run_id, results, stats, sha)
    except Exception as exc:
        store.finish(run_id, [], {}, sha, str(exc))
        raise
    return store.get(run_id)
