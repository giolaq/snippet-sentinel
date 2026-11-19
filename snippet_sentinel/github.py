import base64
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

from .scan import github_url
from .storage import Store


def replace_fence(text, fix):
    lines = text.splitlines(keepends=True)
    start = fix['line'] - 1
    if start < 1 or start > len(lines):
        return None
    fence = re.fullmatch(r'([ \t]*)(`{3,}|~{3,})[^\r\n]*\r?\n?', lines[start - 1])
    if not fence:
        return None
    indent, marker = fence.groups()
    end = start
    while end < len(lines) and not re.fullmatch(r'\s*' + re.escape(marker[0]) + '{' + str(len(marker)) + r',}\s*', lines[end]):
        end += 1
    if end == len(lines):
        return None
    body = []
    for line in lines[start:end]:
        line = line.rstrip('\r\n')
        remove = min(len(indent), len(line) - len(line.lstrip()))
        body.append(line[remove:])
    if '\n'.join(body) != fix['original']:
        return None
    newline = '\r\n' if lines[start - 1].endswith('\r\n') else '\n'
    replacement = [indent + line + newline for line in fix['proposed'].split('\n')]
    return ''.join(lines[:start] + replacement + lines[end:])


class GitHub:
    def __init__(self, token=None, api_base='https://api.github.com'):
        self.token = token or os.getenv('GITHUB_TOKEN') or os.getenv('GH_TOKEN')
        if not self.token:
            raise ValueError('Set GITHUB_TOKEN (or GH_TOKEN) to open a fix PR')
        self.api_base = api_base.rstrip('/')

    def api(self, path, data=None):
        request = Request(self.api_base + path, data=json.dumps(data).encode() if data is not None else None,
                          headers={'Authorization': 'Bearer ' + self.token, 'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json'})
        with urlopen(request, timeout=60) as response:
            return json.load(response)

    def git(self, root, *args, auth=False):
        env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0'}
        if auth:
            encoded = base64.b64encode(('x-access-token:' + self.token).encode()).decode()
            env.update(GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='http.https://github.com/.extraheader',
                       GIT_CONFIG_VALUE_0='AUTHORIZATION: basic ' + encoded)
        result = subprocess.run(['git', '-C', str(root), *args], env=env, capture_output=True, text=True, timeout=180)
        if result.returncode:
            if auth and '403' in result.stderr:
                raise PermissionError('GitHub push denied (403)')
            raise RuntimeError('Git operation failed: ' + args[0])
        return result.stdout.strip()

    def open_pr(self, run, fixes):
        repo = github_url(run['repo'])
        slug = repo.removeprefix('https://github.com/')
        verified = [f for f in fixes if f['verified']]
        if not verified:
            raise ValueError('No verified fixes available')
        if not re.fullmatch(r'[0-9a-fA-F]{40,64}', run['ref'] or ''):
            raise ValueError('Run has no scanned commit SHA')
        metadata = self.api('/repos/' + slug)
        branch = 'sentinel/fix-' + str(run['id'])
        applied, skipped = [], []
        with tempfile.TemporaryDirectory(prefix='sentinel-fix-') as directory:
            root = Path(directory)
            self.git(root, 'clone', '--no-checkout', '--', repo, '.')
            self.git(root, 'checkout', '-b', branch, run['ref'])
            for fix in sorted(verified, key=lambda f: f['line'], reverse=True):
                path = (root / fix['file']).resolve()
                if not path.is_relative_to(root) or not path.is_file() or (root / fix['file']).is_symlink():
                    skipped.append(f"{fix['file']}:{fix['line']} (unsafe or missing file)")
                    continue
                text = path.read_bytes().decode('utf-8')
                changed = replace_fence(text, fix)
                if changed is None:
                    skipped.append(f"{fix['file']}:{fix['line']} (original body mismatch)")
                    continue
                path.write_bytes(changed.encode('utf-8'))
                self.git(root, 'add', '--', fix['file'])
                applied.append(fix)
            if not applied:
                raise ValueError('No verified fixes could be applied: ' + '; '.join(skipped))
            self.git(root, '-c', 'user.name=Snippet Sentinel', '-c', 'user.email=sentinel@users.noreply.github.com', 'commit', '-m', 'Fix verified documentation snippets')
            head = branch
            try:
                self.git(root, 'push', repo, branch, auth=True)
            except PermissionError:
                fork = self.api('/repos/' + slug + '/forks', {})
                fork_repo = github_url('https://github.com/' + fork['full_name'])
                self.git(root, 'push', fork_repo, branch, auth=True)
                head = fork['owner']['login'] + ':' + branch
            body = 'Sandbox-verified snippet fixes from run #' + str(run['id']) + '.\n'
            for fix in applied:
                link = f"{repo}/blob/{run['ref']}/{quote(fix['file'])}?plain=1#L{fix['line']}"
                body += f"\n- [{fix['file']}:{fix['line']}]({link}) — {fix['proposer']}\n\n```text\n{fix['error_before'].replace('```', '~~~')}\n```\n"
            if skipped:
                body += '\nSkipped:\n' + '\n'.join('- ' + item for item in skipped)
            pr = self.api('/repos/' + slug + '/pulls', {'title': 'Fix broken documentation snippets', 'head': head, 'base': metadata['default_branch'], 'body': body})
        return pr['html_url'], skipped


def open_fix_pr(run_id, db='sentinel.db', client=None, claimed=False):
    store = Store(db)
    job = store.fix_job(run_id)
    if not job:
        raise ValueError('No verified fixes available; propose fixes first')
    if job['pr_url']:
        return job
    if not claimed and not store.claim_fix_pr(run_id):
        return job
    try:
        url, skipped = (client or GitHub()).open_pr(store.get(run_id), job['results'])
        store.update_fix_job(run_id, status='done', pr_url=url, error='; '.join(skipped))
    except Exception as exc:
        store.update_fix_job(run_id, status='error', error=str(exc))
    return store.fix_job(run_id)
