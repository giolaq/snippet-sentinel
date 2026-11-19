import difflib
import json
import os
import re
import sys
from urllib.request import Request, urlopen

from . import runner
from .classifier import BROKEN
from .dependencies import BUILTINS, javascript_mode
from .extractor import Snippet
from .storage import Store

ABCS = 'Awaitable Coroutine AsyncIterable AsyncIterator AsyncGenerator Hashable Iterable Iterator Generator Reversible Sized Container Collection Callable Set MutableSet Mapping MutableMapping MappingView KeysView ItemsView ValuesView Sequence MutableSequence ByteString'.split()


class Rules:
    name = 'deterministic rules'

    def propose(self, result):
        code, error = result['code'], result.get('stderr', '')
        if result['lang'] == 'python':
            missing = re.search(r"NameError: name '([\w]+)' is not defined", error)
            if missing and missing[1] in sys.stdlib_module_names:
                return f'import {missing[1]}\n' + code
            if 'SyntaxError' in error:
                fixed = re.sub(r'^(\s*)print (?!\()(.*)$', r'\1print(\2)', code, flags=re.M)
                if fixed != code:
                    return fixed
            if 'collections' in error:
                fixed = re.sub(r'\bcollections\.(' + '|'.join(ABCS) + r')\b', r'collections.abc.\1', code)
                if fixed != code:
                    return 'import collections.abc\n' + fixed
                fixed = re.sub(r'from collections import (' + '|'.join(ABCS) + r')\b', r'from collections.abc import \1', code)
                if fixed != code:
                    return fixed
        else:
            missing = re.search(r'ReferenceError: (\w+) is not defined', error)
            if missing and missing[1] in BUILTINS:
                name = missing[1]
                prefix = f"import {name} from 'node:{name}';" if javascript_mode(code) else f"const {name} = require('node:{name}');"
                return prefix + '\n' + code
            if 'require is not defined in ES module scope' in error:
                fixed = re.sub(r'''\b(?:const|let|var)\s+(\w+)\s*=\s*require\(\s*(['"])([^'"]+)\2\s*\)''', r"import \1 from '\3'", code)
                if fixed != code:
                    return fixed
        return None


class LLM:
    name = 'LLM'

    def propose(self, result):
        anthropic, openai = os.getenv('ANTHROPIC_API_KEY'), os.getenv('OPENAI_API_KEY')
        if not (anthropic or openai):
            return None
        prompt = f"Fix this {result['lang']} documentation snippet. Return code only, no explanation.\nCODE:\n{result['code']}\nERROR:\n{result.get('stderr', '')[-4000:]}"
        messages = [{'role': 'user', 'content': prompt}]
        if anthropic:
            url = 'https://api.anthropic.com/v1/messages'
            headers = {'x-api-key': anthropic, 'anthropic-version': '2023-06-01'}
            payload = {'model': 'claude-sonnet-4-20250514', 'max_tokens': 4096, 'messages': messages}
        else:
            url = 'https://api.openai.com/v1/chat/completions'
            headers = {'Authorization': 'Bearer ' + openai}
            payload = {'model': 'gpt-4o-mini', 'messages': messages}
        request = Request(url, data=json.dumps(payload).encode(), headers={**headers, 'Content-Type': 'application/json'})
        with urlopen(request, timeout=60) as response:
            data = json.load(response)
        code = data['content'][0]['text'] if anthropic else data['choices'][0]['message']['content']
        return re.sub(r'\n?```\s*$', '', re.sub(r'^```[^\n]*\n', '', code.strip()))


def fix_run(run_id, db='sentinel.db', proposers=None):
    store = Store(db)
    run = store.get(run_id)
    if not run or not run['finished'] or run['error']:
        raise ValueError('A successfully completed scan is required')
    if not store.ensure_fix_job(run_id):
        job = store.fix_job(run_id)
        if job['status'] != 'pending':
            return job
    store.update_fix_job(run_id, status='running', error='')
    try:
        for result in run['results']:
            if result['status'] not in BROKEN:
                continue
            fix = {'file': result['file'], 'line': result['line'], 'original': result['code'],
                   'proposed': None, 'proposer': '', 'verified': False, 'output': '',
                   'error_before': result.get('stderr', '')[-2000:]}
            for proposer in ([Rules(), LLM()] if proposers is None else proposers):
                try:
                    candidate = proposer.propose(result)
                    if candidate is None or candidate == result['code']:
                        continue
                    fix.update(proposed=candidate, proposer=proposer.name)
                    snippet = Snippet(result['id'], result['file'], result['line'], result['lang'], candidate)
                    verification = runner.run(snippet, result['runner'])
                    fix.update(verified=verification['status'] == 'pass', output=verification.get('stderr', '')[-2000:])
                    if fix['verified']:
                        break
                except Exception as exc:
                    fix['output'] = str(exc)[-2000:]
            fix['diff'] = '\n'.join(difflib.unified_diff(fix['original'].splitlines(), (fix['proposed'] or fix['original']).splitlines(), fromfile='before', tofile='after', lineterm=''))
            store.save_fix(run_id, fix)
        store.update_fix_job(run_id, status='done')
    except Exception as exc:
        store.update_fix_job(run_id, status='error', error=str(exc))
    return store.fix_job(run_id)
