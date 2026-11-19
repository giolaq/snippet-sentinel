import ast
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from .classifier import classify, error_summary, final_error, undefined_name
from .dependencies import TSX, dependencies, javascript_mode, pinned, python_package, script_name

QUOTED_PATH = r"""(?:'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")"""
ERROR_PATHS = re.compile(rf'({QUOTED_PATH})(?: -> ({QUOTED_PATH}))?')


class InfrastructureError(RuntimeError):
    pass


def missing_placeholder_path(code, message, frames):
    _, separator, filenames = message.partition(': ')
    if separator:
        match = ERROR_PATHS.fullmatch(filenames)
        if match:
            for filename in filter(None, match.groups()):
                try:
                    path = ast.literal_eval(filename)
                except (SyntaxError, ValueError):
                    continue
                if path.startswith('/path/to/'):
                    return path
        return ''
    if not frames or not frames[-1][0].endswith('/ssl.py'):
        return ''
    lines = [int(line) for file, line in frames if file.endswith('/snippet.py')]
    if not lines:
        return ''
    calls = [node for node in ast.walk(ast.parse(code)) if isinstance(node, ast.Call)
             and node.lineno <= lines[-1] <= node.end_lineno]
    if len(calls) != 1 or ast.unparse(calls[0].func) != 'ssl.create_default_context':
        return ''
    keywords = {keyword.arg: keyword.value for keyword in calls[0].keywords}
    cafile = keywords.get('cafile')
    if not keywords.keys() & {None, 'capath', 'cadata'} and isinstance(cafile, ast.Constant):
        path = cafile.value
        if isinstance(path, str) and path.startswith('/path/to/'):
            return path
    return ''


def documented_failure(snippet, status, output):
    if status not in {'syntax_error', 'runtime_error', 'missing_dependency', 'needs_credentials', 'needs_context'}:
        return ''
    error = final_error(output)
    if not error:
        return ''
    name, message = error['name'], error['message'] or ''
    if name == snippet.expected_error:
        return f'demonstrates {name} as documented'
    frames = re.findall(r'^  File "([^"\n]+)", line (\d+), in ', output[:error.start()], re.M)
    if name == 'NameError':
        missing = re.fullmatch(
            r"name '(\w+)' is not defined(?:\. (?:Did you mean: .+|Did you forget to import '\1'\?))?",
            message,
        )
        if missing and missing[1] in snippet.context_names:
            return f'requires earlier setup: {missing[1]}'
        if (missing and 'Did you mean:' not in message and missing[1] in snippet.illustrative_names
                and frames and frames[-1][0].endswith('/snippet.py')):
            return f'requires example setup: {missing[1]}'
    if name == 'SyntaxError' and message == "'await' outside function" and snippet.context_names:
        return 'async fragment requiring earlier setup: ' + ', '.join(snippet.context_names)
    if name == 'FileNotFoundError' and snippet.lang == 'python':
        path = missing_placeholder_path(snippet.code, message, frames)
        if path:
            return f'requires replacing the placeholder path: {path}'
    return ''


def illustrative_dependency(snippet, output):
    if snippet.lang != 'python':
        return ''
    unavailable = {re.sub(r'[-_.]+', '-', name.lower()) for name in re.findall(
        r'^ERROR: No matching distribution found for ([\w.-]+)$', output, re.M)}
    if not unavailable:
        return ''
    tree = ast.parse(snippet.code)
    imported = {alias.asname or alias.name.split('.')[0]: alias.name
                for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
                if re.sub(r'[-_.]+', '-', python_package(alias.name).lower()) in unavailable}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            alias = node.func.value.id
            if alias in imported and any(isinstance(arg, ast.Constant) and arg.value is Ellipsis for arg in node.args):
                return f'illustrative dependency with placeholder arguments: {imported[alias]}'
    return ''


def command(args, cwd, timeout, env=None):
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(args, cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        timed_out = False
        try:
            process.wait(timeout=max(.01, timeout))
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        output.seek(0, 2)
        size = output.tell()
        output.seek(max(0, size - 16000))
        return output.read().decode(errors='replace'), process.returncode, timed_out


def run(snippet, runner='docker', timeout=60, pins=()):
    result = {**snippet.dict(), 'runner': runner, 'versions': {}, 'stderr': '', 'exit_code': None}
    if snippet.reason:
        return {**result, 'status': 'skipped'}
    packages = dependencies(snippet.code, snippet.lang)
    specs = pinned(packages, pins, snippet.lang)
    python = snippet.lang == 'python'
    esm = javascript_mode(snippet.code) if not python else False
    filename = script_name(snippet.code, snippet.lang)
    transpile = filename.endswith(('.ts', '.tsx', '.jsx'))
    name = 'sentinel-' + uuid.uuid4().hex
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='sentinel-') as directory:
        path = Path(directory)
        path.joinpath(filename).write_text(snippet.code)
        # A tiny worker keeps package discovery and execution identical across backends.
        worker = '''import json, subprocess, sys
packages, specs, python, filename = json.loads(sys.argv[1])[:4]
if specs:
    install = [sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check', *specs] if python else ['npm', 'install', '--no-audit', '--no-fund', '--', *specs]
    p = subprocess.run(install, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if p.returncode:
        print('SENTINEL_INSTALL_FAILED', flush=True)
        print(p.stdout, flush=True)
        sys.exit(p.returncode)
if python:
    import importlib.metadata
    versions = {p: importlib.metadata.version(p) for p in packages}
else:
    p = subprocess.run(['npm', 'ls', '--json', '--depth=0'], capture_output=True, text=True)
    data = json.loads(p.stdout or '{}')
    versions = {k: v.get('version', '?') for k, v in data.get('dependencies', {}).items() if k in packages}
print('SENTINEL_VERSIONS=' + json.dumps(versions), flush=True)
sys.exit(subprocess.call([sys.executable if python else 'node', filename]))
'''
        path.joinpath('worker.py').write_text(worker)
        payload = json.dumps([packages, specs, python, filename, transpile, esm])
        if not python:
            js_worker = '''const {spawnSync} = require('node:child_process');
const fs = require('node:fs');
const [packages,specs,,filename,transpile,esm] = JSON.parse(process.argv[2]);
const installs = transpile ? [...specs, %r] : specs;
if (transpile) fs.writeFileSync('package.json', JSON.stringify({type: esm ? 'module' : 'commonjs'}));
if (installs.length) {
 const p = spawnSync('npm', ['install','--no-audit','--no-fund','--',...installs], {encoding:'utf8'});
 if(p.status !== 0) { console.log('SENTINEL_INSTALL_FAILED'); console.log(p.stdout, p.stderr); process.exit(p.status || 1); }
}
const p = spawnSync('npm',['ls','--json','--depth=0'],{encoding:'utf8'});
const deps = JSON.parse(p.stdout || '{}').dependencies || {};
console.log('SENTINEL_VERSIONS='+JSON.stringify(Object.fromEntries(packages.filter(x=>deps[x]).map(x=>[x,deps[x].version]))));
const exec = (file, options) => spawnSync(transpile ? 'node_modules/.bin/tsx' : 'node', [file], options);
let r;
if (esm) {
 r = exec(filename, {stdio: 'inherit'});
} else {
 r = exec(filename, {stdio: ['inherit', 'inherit', 'pipe'], encoding: 'utf8', maxBuffer: 16 * 1024 * 1024});
 if (r.status !== 0 && /SyntaxError: (?:await is only valid in async functions and the top level bodies of modules|Unexpected reserved word)|Top-level await is currently not supported with the "cjs" output format/.test(r.stderr || '')) {
  const lines = fs.readFileSync(filename, 'utf8').split('\\n');
  const hoisted = lines.filter(line => /^\\s*import\\b(?!\\s*[(.])/.test(line));
  const body = lines.filter(line => !/^\\s*import\\b(?!\\s*[(.])/.test(line));
  const wrapped = 'snippet-async' + filename.slice(filename.lastIndexOf('.'));
  fs.writeFileSync(wrapped, hoisted.join('\\n') + '\\n(async () => {\\n' + body.join('\\n') + '\\n})().catch(error => { console.error(error); process.exitCode = 1; });\\n');
  r = exec(wrapped, {stdio: 'inherit'});
 } else if (r.stderr) {
  process.stderr.write(r.stderr);
 }
}
if (r.error) console.error(r.error);
process.exit(r.status === null ? 1 : r.status);
'''
            path.joinpath('worker.cjs').write_text(js_worker % TSX)
        try:
            if runner == 'docker':
                # Copy instead of bind mounting: works with the dashboard's socket mount.
                image = 'python:3.12-slim' if python else 'node:22-slim'
                out, rc, expired = command(['docker', 'create', '--name', name, '--memory', '512m', '--cpus', '1',
                    '--pids-limit', '256', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                    '--workdir', '/work', image, 'python' if python else 'node',
                    'worker.py' if python else 'worker.cjs', payload], directory, timeout)
                if rc or expired:
                    raise InfrastructureError(f'Docker container creation failed (image/daemon unavailable): {out or "timed out"}')
                out, rc, expired = command(['docker', 'cp', directory + '/.', name + ':/work'], directory, timeout - (time.monotonic() - started))
                if rc or expired:
                    raise InfrastructureError(f'Docker container setup failed: {out or "timed out"}')
                out, rc, expired = command(['docker', 'start', '-a', name], directory, timeout - (time.monotonic() - started))
                if not expired:
                    inspect = subprocess.run(['docker', 'inspect', '-f', '{{json .State}}', name], capture_output=True, text=True)
                    if inspect.returncode:
                        raise InfrastructureError(f'Docker container inspection failed: {inspect.stderr}')
                    state = json.loads(inspect.stdout)
                    if state.get('Error') or state.get('Status') != 'exited':
                        raise InfrastructureError(f'Docker container start failed: {state.get("Error") or out}')
                    rc = state['ExitCode']
            else:
                env = {'PATH': os.environ.get('PATH', ''), 'HOME': directory, 'LANG': 'C.UTF-8'}
                executable = sys.executable
                if python:
                    out, rc, expired = command([sys.executable, '-m', 'venv', str(path / 'venv')], directory, timeout, env)
                    if rc or expired:
                        return {**result, 'status': 'timeout' if expired else 'runtime_error', 'stderr': out, 'exit_code': rc}
                    executable = str(path / 'venv/bin/python')
                elif not shutil.which('node') or not shutil.which('npm'):
                    return {**result, 'status': 'runtime_error', 'stderr': 'Local JavaScript runner requires node and npm (UNSANDBOXED).'}
                args = [executable, 'worker.py', payload] if python else ['node', 'worker.cjs', payload]
                out, rc, expired = command(args, directory, timeout - (time.monotonic() - started), env)
            lines = []
            for line in out.splitlines():
                if line.startswith('SENTINEL_VERSIONS='):
                    try:
                        result['versions'] = json.loads(line.split('=', 1)[1])
                    except ValueError:
                        pass
                else:
                    lines.append(line)
            install_failed = 'SENTINEL_INSTALL_FAILED' in lines
            output = '\n'.join(lines)
            status = classify(output, rc, expired, install_failed, snippet.code)
            if install_failed:
                reason = illustrative_dependency(snippet, output) if status == 'missing_dependency' else ''
            else:
                reason = documented_failure(snippet, status, output)
            if reason:
                status = 'skipped'
            elif status == 'needs_context':
                reason = (f'Uses `{undefined_name(error_summary(output), snippet.code)}` without defining it: '
                          'likely continues an earlier example or expects a browser page.')
            result.update(status=status, reason=reason, stderr=output[-8000:], exit_code=rc)
        except OSError as exc:
            if runner == 'docker':
                raise InfrastructureError(f'Docker unavailable: {exc}') from exc
            result.update(status='runtime_error', stderr=str(exc))
        finally:
            if runner == 'docker':
                try:
                    subprocess.run(['docker', 'rm', '-f', name], capture_output=True)
                except OSError:
                    pass
    return result
