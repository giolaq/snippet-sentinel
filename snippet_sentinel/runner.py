import ast
import difflib
import json
import os
import re
import selectors
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import threading
import uuid
from pathlib import Path

from .classifier import BROKEN, classify, error_summary, final_error, context_reason
from .dependencies import TSX, dependencies, javascript_mode, pinned, python_package, script_name
from .network_guard import MARKER
from .network_diagnostics import blocked_hosts, dead_host

QUOTED_PATH = r"""(?:'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")"""
ERROR_PATHS = re.compile(rf'({QUOTED_PATH})(?: -> ({QUOTED_PATH}))?')
from .extractor import runnable_code


# Runs a Python snippet as __main__, allowing top-level await like the asyncio REPL and notebooks.
PYTHON_RUN = '''def _sentinel_main():
    # Only modules Python loads at startup (sys, os) are imported, so the snippet sees a fresh interpreter.
    import os, sys
    filename = os.path.abspath(sys.argv[1])
    source = open(filename).read()
    coroutine = False
    try:
        code = compile(source, filename, 'exec')
    except SyntaxError as error:
        try:
            code = compile(source, filename, 'exec', flags=0x2000)  # ast.PyCF_ALLOW_TOP_LEVEL_AWAIT
        except SyntaxError:
            code = None
        if code is None or not code.co_flags & 0x80:  # inspect.CO_COROUTINE
            raise error from None
        coroutine = True
    namespace = sys.modules['__main__'].__dict__
    builtins = namespace['__builtins__']
    namespace.clear()
    namespace.update(__name__='__main__', __file__=filename, __builtins__=builtins)
    sys.argv = [filename]
    try:
        if coroutine:
            import asyncio
            asyncio.run(eval(code, namespace))
        else:
            exec(code, namespace)
    except NameError as error:
        # Name a missing import: the name is a public export of an installed package.
        for module_name, module in sorted(sys.modules.items()):
            if '.' not in module_name and 'site-packages' in (getattr(module, '__file__', None) or ''):
                try:
                    if callable(getattr(module, error.name, None)):  # functions/classes, not submodules or values
                        print('SENTINEL_EXPORTED_BY=' + module_name, file=sys.stderr, flush=True)
                        break
                except Exception:
                    pass
        raise


_sentinel_main()
'''


class InfrastructureError(RuntimeError):
    def __init__(self, diagnostics):
        super().__init__("Docker couldn't run this snippet — start Docker (or check DOCKER_HOST) and retry.\n" + str(diagnostics))


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
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return ''
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
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


PLACEHOLDER_HOST = re.compile(r'''(?:https?://|['"])((?:[\w-]+\.)*example\.(?:com|org|net))(?![\w-]|\.\w)''')


def javascript_placeholder(code, error):
    """A failure caused by a documented placeholder the reader is meant to replace."""
    path = re.search(r"ENOENT: no such file or directory, \w+ '(/path/to/[^']+)'", error)
    if path and path.group(1) in code:
        return f'requires replacing the placeholder path: {path.group(1)}'
    host = PLACEHOLDER_HOST.search(code)
    if host and re.search(r'ENOTFOUND|ECONNREFUSED|fetch failed|\b40[04]\b|Invalid JSON', error):
        return f'requires replacing the placeholder host: {host.group(1)}'
    return ''


def documented_failure(snippet, status, output):
    if status not in {'syntax_error', 'runtime_error', 'missing_dependency', 'needs_credentials', 'needs_context'}:
        return ''
    error = final_error(output)
    if not error:
        return ''
    name, message = error['name'], error['message'] or ''
    if name == snippet.expected_error or mentions_error(snippet, name):
        return f'demonstrates {name} as documented'
    if snippet.lang != 'python':
        return javascript_placeholder(snippet.code, error[0])
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
        path = missing_placeholder_path(runnable_code(snippet), message, frames)
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
    try:
        tree = ast.parse(runnable_code(snippet))
    except SyntaxError:
        return ''
    imported = {alias.asname or alias.name.split('.')[0]: alias.name
                for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
                if re.sub(r'[-_.]+', '-', python_package(alias.name).lower()) in unavailable}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            alias = node.func.value.id
            if alias in imported and any(isinstance(arg, ast.Constant) and arg.value is Ellipsis for arg in node.args):
                return f'illustrative dependency with placeholder arguments: {imported[alias]}'
    return ''


def mentions_error(snippet, name):
    """The snippet itself says it fails: `# Raises ValueError` or the error in a transcript's expected output."""
    if snippet.lang != 'python' or not name:
        return False
    transcript = runnable_code(snippet) != snippet.code
    for line in snippet.code.splitlines():
        line = line.strip()
        if '#' in line and re.search(rf'\b{name}\b', line.split('#', 1)[1]):
            return True
        if transcript and not line.startswith(('>>>', '...')) and re.search(rf'\b{name}\b', line):
            return True
    return False


PLACEHOLDER_LITERAL = re.compile(r'(?i)\b(?:your|my)[-_][\w./-]*|<[\w -]+>')
READER_INPUT = re.compile(r"Provided path: '([^']+)' is not a (?:file|directory)|Source must be an existing (?:local )?directory: '?([^'\s]+?)'?\.?$"
                          r"|No such file or directory: '((?:\./)?path/to/[^']+)'")


def python_inconclusive(code, status, output, exit_code=None):
    """(status, reason) for a Python failure that says nothing about the docs: the reader's files, IDs, extras or server."""
    if status == 'timeout':
        if re.search(r'Waiting for authorization|Enter your token|token \(input will not be visible\)', output):
            return 'needs_credentials', 'Waits for an interactive login.'
        if len(re.findall(r'\b(\d+)/\d+ \[', output)) > 1 and re.search(r'Fetching \d+ files|Downloading', output):
            return 'skipped', 'Still downloading at the time limit; not evaluated.'
        return None
    if re.search(r'^(?:\w+\.)*CacheNotFound: Cache directory not found', output, re.M):
        return 'needs_context', 'Inspects the reader\'s local Hub cache, which is empty in a fresh sandbox.'
    error = final_error(output)
    if not error:
        if status == 'runtime_error' and not output.strip() and exit_code is not None and (exit_code < 0 or exit_code >= 128):
            return 'skipped', 'Killed without output, likely by the sandbox memory limit; not evaluated.'
        return None
    name, message = error['name'], error['message'] or ''
    frames = re.findall(r'^  File "([^"\n]+)", line \d+, in ', output[:error.start()], re.M)
    if (name.endswith('MissingDependencyError') or name in {'ImportError', 'ModuleNotFoundError'}
            and re.search(r'pip install|to be installed|install it\b|Use pip\b', message, re.I)) and frames and 'site-packages' in frames[-1]:
        return 'needs_context', f'The SDK needs an optional dependency the docs ask readers to install: {message[:120]}'
    if path := READER_INPUT.search(message):
        return 'needs_context', f'Needs a local file or directory the reader provides: {next(filter(None, path.groups()))}'
    if re.search(r'\b404\b|not found', message, re.I):
        literals = [node.value for node in ast.walk(parse(code)) if isinstance(node, ast.Constant) and isinstance(node.value, str)]
        placeholder = next((m[0] for value in literals for m in PLACEHOLDER_LITERAL.finditer(value) if m[0] in message), '')
        if placeholder:
            return 'needs_context', f'Requires replacing the placeholder: {placeholder}'
    if re.search(r'Connection refused|ConnectError', name + message) and re.search(r'localhost|127\.0\.0\.1|0\.0\.0\.0', code):
        return 'needs_context', 'Talks to a server the reader runs locally.'
    if name == 'NameError' and (missing := re.match(r"name '(\w+)' is not defined", message)):
        return reader_value(code, missing[1])
    return None


def parse(code):
    try:
        return ast.parse(code)
    except SyntaxError:
        return ast.Module(body=[], type_ignores=[])


def reader_value(code, missing):
    """`update_job_labels(job_id=job_id)`: a value the reader supplies, not a missing import (called, `.attr`, `[...]`)."""
    tree = parse(code)
    uses = [node for node in ast.walk(tree) if isinstance(node, ast.Name) and node.id == missing]
    if not uses:
        return None
    structural = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    structural |= {id(node.value) for node in ast.walk(tree) if isinstance(node, (ast.Attribute, ast.Subscript))}
    structural |= {id(node.returns) for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    structural |= {id(node.annotation) for node in ast.walk(tree) if isinstance(node, (ast.arg, ast.AnnAssign))}
    if any(id(node) in structural for node in uses) and not re.match(r'(?:My|Your)[A-Z][a-z]\w*$', missing):
        return None
    bound = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load)}
    bound |= {node.arg for node in ast.walk(tree) if isinstance(node, ast.arg)}
    bound |= {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    bound |= {(alias.asname or alias.name).split('.')[0] for node in ast.walk(tree)
              if isinstance(node, (ast.Import, ast.ImportFrom)) for alias in node.names}
    if missing in bound:
        return None
    names = bound | {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    if difflib.get_close_matches(missing, names - {missing}, cutoff=.8):
        return None
    return 'needs_context', f'Uses `{missing}`, a value the reader supplies (an ID or input defined outside the docs).'


OUTPUT_LIMIT = 2 * 1024 * 1024
SANDBOX_LABEL = 'snippet-sentinel.sandbox'
_active = set()
_lock = threading.Lock()
_stopping = threading.Event()


def portable_start(pid):
    try:
        result = subprocess.run(['ps', '-p', str(pid), '-o', 'lstart='], capture_output=True, text=True, timeout=2,
                                env={**os.environ, 'TZ': 'UTC', 'LC_ALL': 'C'})
        return result.stdout.strip() if result.returncode == 0 else ''
    except (OSError, subprocess.SubprocessError):
        return ''


def owner_identity():
    try:
        started = Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19]
        return f'{os.getpid()}:{started}'
    except (OSError, IndexError):
        started = portable_start(os.getpid())
        return f'{os.getpid()}:ps-{started}' if started else f'{os.getpid()}:unknown-{socket.gethostname()}'


def owner_namespace():
    try:
        return os.readlink('/proc/self/ns/pid')
    except OSError:
        return f'host:{socket.gethostname()}'


def owner_container():
    try:
        mounts = Path('/proc/self/mountinfo').read_text().splitlines()
    except OSError:
        return ''
    for line in mounts:
        fields = line.split()
        if len(fields) > 4 and fields[4] == '/etc/hostname':
            match = re.search(r'/containers/([a-f0-9]{64})/hostname$', fields[3])
            if match:
                return match[1]
    return ''


def cleanup_stale():
    try:
        _cleanup_stale()
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError):
        pass


def _cleanup_stale():
    namespace = owner_namespace()
    listing = subprocess.run(['docker', 'ps', '-aq', '--filter', f'label={SANDBOX_LABEL}=true'],
                             capture_output=True, text=True, timeout=10)
    for name in listing.stdout.split():
        info = subprocess.run(['docker', 'inspect', '-f', '{{json .Config.Labels}}', name],
                              capture_output=True, text=True, timeout=10)
        if info.returncode:
            continue
        labels = json.loads(info.stdout)
        if namespace and labels.get('snippet-sentinel.namespace') == namespace:
            try:
                pid, started = labels.get('snippet-sentinel.owner', '').split(':', 1)
                pid = int(pid)
                if pid <= 0:
                    continue
                if namespace.startswith('host:'):
                    os.kill(pid, 0)
                    current = portable_start(pid)
                    live = not (started.startswith('ps-') and current and started[3:] != current)
                else:
                    live = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19] == started
            except ProcessLookupError:
                live = False
            except FileNotFoundError:
                live = False
            except (ValueError, OSError, IndexError):
                live = True
        elif labels.get('snippet-sentinel.owner-container'):
            parent = subprocess.run(['docker', 'inspect', '-f', '{{.State.Running}}', labels['snippet-sentinel.owner-container']],
                                    capture_output=True, text=True, timeout=10)
            if parent.returncode and 'No such' not in parent.stderr:
                continue
            live = parent.returncode == 0 and parent.stdout.strip() == 'true'
        else:
            continue
        if not live:
            subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=10)


def cleanup_active():
    _stopping.set()
    with _lock:
        names = list(_active)
    if names:
        try:
            subprocess.run(['docker', 'rm', '-f', *names], capture_output=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            pass


def stop_sandboxes(signum, frame):
    cleanup_active()
    raise SystemExit(128 + signum)


def install_signal_handlers():
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, stop_sandboxes)


def command(args, cwd, timeout, env=None, stdin=None):
    process = subprocess.Popen(args, cwd=cwd, env=env, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               start_new_session=True)
    output = bytearray()
    timed_out = False
    exceeded = False
    deadline = time.monotonic() + max(.01, timeout)
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                if not selector.select(remaining):
                    timed_out = True
                    break
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    process.wait(timeout=max(.01, deadline - time.monotonic()))
                    break
                if len(output) + len(chunk) > OUTPUT_LIMIT:
                    exceeded = True
                    break
                output.extend(chunk)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            if process.poll() is None or timed_out or exceeded:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait()
            process.stdout.close()
    decoded = output.decode(errors='replace')
    tail = output[-16000:].decode(errors='replace')
    error = final_error(decoded)
    if error and error[0] not in tail:
        tail = error[0][:512] + '\n' + tail
    if MARKER.encode() in output and MARKER not in tail:
        tail = MARKER + '\n' + tail
    hosts = blocked_hosts(decoded)
    if hosts:
        tail = ''.join(MARKER + ' ' + json.dumps(host) + '\n' for host in hosts) + tail
    if exceeded:
        tail += '\nSENTINEL_OUTPUT_LIMIT: output limit exceeded (2 MiB)'
    return tail, process.returncode, timed_out


def run(snippet, runner='docker', timeout=60, pins=(), allow_network=False):
    result = {**snippet.dict(), 'runner': runner, 'versions': {}, 'stderr': '', 'exit_code': None}
    if snippet.reason:
        return {**result, 'status': 'skipped'}
    code = runnable_code(snippet)
    packages = dependencies(code, snippet.lang)
    specs = pinned(packages, pins, snippet.lang)
    python = snippet.lang == 'python'
    esm = javascript_mode(code) if not python else False
    filename = script_name(code, snippet.lang)
    transpile = filename.endswith(('.ts', '.tsx', '.jsx'))
    name = 'sentinel-' + uuid.uuid4().hex
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='sentinel-') as directory:
        path = Path(directory)
        path.joinpath(filename).write_text(code)
        if runner == 'docker' and not allow_network:
            path.joinpath('network_guard.py').write_text(Path(__file__).with_name('network_guard.py').read_text())
            path.joinpath('network_guard.cjs').write_text(Path(__file__).with_name('network_guard.cjs').read_text())
            path.joinpath('sitecustomize.py').write_text("import os\nif os.environ.get('SENTINEL_NETWORK_DISABLED') == '1':\n from network_guard import install\n install()\n")
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
sys.exit(subprocess.call([sys.executable, 'sentinel_run.py', filename] if python else ['node', filename]))
'''
        path.joinpath('worker.py').write_text(worker)
        if python:
            path.joinpath('sentinel_run.py').write_text(PYTHON_RUN)
        payload = json.dumps([packages, specs, python, filename, transpile, esm])
        if not python:
            js_worker = '''const {spawnSync} = require('node:child_process');
const fs = require('node:fs');
const [packages,specs,,filename,transpile,esm,prepared] = JSON.parse(process.argv[2]);
const installs = transpile && !prepared ? [...specs, %r] : specs;
if (transpile) fs.writeFileSync('package.json', JSON.stringify({type: esm ? 'module' : 'commonjs'}));
if (installs.length) {
 const p = spawnSync('npm', ['install','--no-audit','--no-fund','--',...installs], {encoding:'utf8'});
 if(p.status !== 0) { console.log('SENTINEL_INSTALL_FAILED'); console.log(p.stdout, p.stderr); process.exit(p.status || 1); }
}
const p = spawnSync('npm',['ls','--json','--depth=0'],{encoding:'utf8'});
const deps = JSON.parse(p.stdout || '{}').dependencies || {};
console.log('SENTINEL_VERSIONS='+JSON.stringify(Object.fromEntries(packages.filter(x=>deps[x]).map(x=>[x,deps[x].version]))));
const exec = (file, options) => spawnSync('node', [...(transpile ? ['--import', 'tsx'] : []), file], options);
let r;
if (esm) {
 r = exec(filename, {stdio: 'inherit'});
} else {
 r = exec(filename, {stdio: ['inherit', 'inherit', 'pipe', 2], env: {...process.env, SENTINEL_NETWORK_MARKER_FD: '3'}, encoding: 'utf8', maxBuffer: 2 * 1024 * 1024});
 if (r.error && r.error.code === 'ENOBUFS') {
  fs.writeSync(2, 'SENTINEL_OUTPUT_LIMIT: output limit exceeded (2 MiB)\\n');
  process.exit(1);
 }
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
                if _stopping.is_set():
                    raise InfrastructureError('Scan interrupted')
                with _lock:
                    _active.add(name)
                image = 'python:3.12-slim' if python else 'node:22-slim'
                out, rc, expired = command(['docker', 'create', '--name', name, '--memory', '512m', '--cpus', '1',
                    '--pids-limit', '256', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                    '--label', f'{SANDBOX_LABEL}=true', '--label', f'snippet-sentinel.owner={owner_identity()}',
                    '--label', f'snippet-sentinel.namespace={owner_namespace()}',
                    '--label', f'snippet-sentinel.owner-container={owner_container()}',
                    '--read-only', '--tmpfs', '/work:rw,exec,nosuid,size=128m',
                    '--tmpfs', '/tmp:rw,noexec,nosuid,size=32m', '--log-driver', 'none',
                    '--env', 'HOME=/tmp', '--env', 'PYTHONPATH=/work:/work/.packages',
                    '--env', 'PATH=/work/.packages/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
                    '--env', 'PYTHONDONTWRITEBYTECODE=1', '--workdir', '/work', image,
                    'python' if python else 'node', '-c' if python else '-e',
                    'import time; time.sleep(86400)' if python else 'setInterval(()=>{},86400000)'], directory, timeout)
                if rc or expired:
                    raise InfrastructureError(f'Docker container creation failed (image/daemon unavailable): {out or "timed out"}')
                if _stopping.is_set():
                    raise InfrastructureError('Scan interrupted')
                out, rc, expired = command(['docker', 'start', name], directory, timeout - (time.monotonic() - started))
                if rc or expired:
                    raise InfrastructureError(f'Docker container start failed: {out or "timed out"}')
                with tempfile.TemporaryFile() as archive:
                    with tarfile.open(fileobj=archive, mode='w') as tar:
                        tar.add(directory, arcname='.')
                    archive.seek(0)
                    out, rc, expired = command(['docker', 'exec', '-i', name, 'tar', '--no-same-owner', '-x', '-C', '/work'],
                                              directory, timeout - (time.monotonic() - started), stdin=archive)
                if rc or expired:
                    raise InfrastructureError(f'Docker container setup failed: {out or "timed out"}')
                install_out = ''
                install_specs = [*specs, TSX] if transpile else specs
                if install_specs:
                    install = ['python', '-m', 'pip', 'install', '--disable-pip-version-check', '--no-cache-dir', '--target', '/work/.packages', *install_specs] if python else ['npm', 'install', '--no-audit', '--no-fund', '--', *install_specs]
                    install_out, rc, expired = command(['docker', 'exec', '--env', 'TMPDIR=/work', name, *install], directory, timeout - (time.monotonic() - started))
                if rc or expired or 'SENTINEL_OUTPUT_LIMIT:' in install_out:
                    out = 'SENTINEL_INSTALL_FAILED\n' + install_out
                else:
                    if not allow_network:
                        out, rc, expired = command(['docker', 'network', 'disconnect', 'bridge', name], directory, timeout - (time.monotonic() - started))
                        if rc or expired:
                            raise InfrastructureError(f'Docker network isolation failed: {out or "timed out"}')
                    payload = json.dumps([packages, [], python, filename, transpile, esm, True])
                    guarded = ['--env', 'SENTINEL_NETWORK_DISABLED=1', '--env', 'NODE_OPTIONS=--require=/work/network_guard.cjs'] if not allow_network else []
                    out, rc, expired = command(['docker', 'exec', *guarded, name, 'python' if python else 'node',
                                               'worker.py' if python else 'worker.cjs', payload], directory, timeout - (time.monotonic() - started))
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
            exported_by = next((line.split('=', 1)[1] for line in lines if line.startswith('SENTINEL_EXPORTED_BY=')), '')
            lines = [line for line in lines if not line.startswith('SENTINEL_EXPORTED_BY=')]
            install_failed = 'SENTINEL_INSTALL_FAILED' in lines
            output = '\n'.join(lines)
            status = classify(output, rc, expired, install_failed, code,
                              network_disabled=runner == 'docker' and not allow_network)
            if status == 'needs_network' and runner == 'docker' and not allow_network:
                host = dead_host(output)
                if host:
                    status = 'runtime_error'
                    output += f'\nhost {host} does not resolve (dead URL in docs)'
            if install_failed:
                reason = illustrative_dependency(snippet, output) if status == 'missing_dependency' else ''
            else:
                reason = documented_failure(snippet, status, output)
            if exported_by and reason.startswith('requires example setup'):
                reason = ''
            inconclusive = (python and not reason and not exported_by and status in BROKEN
                            and python_inconclusive(code, status, output, rc))
            if python and expired and specs and 'SENTINEL_VERSIONS=' not in out and len(out.encode()) < 15000:
                inconclusive = 'skipped', 'Installing dependencies exceeded the time limit; not evaluated.'
            if inconclusive:
                status, reason = inconclusive
            elif reason:
                status = 'skipped'
            elif status == 'needs_context':
                reason = context_reason(output, code)
            if exported_by and status == 'runtime_error' and (error := final_error(output)) and error['name'] == 'NameError':
                missing = re.match(r"name '(\w+)'", error['message'] or '')
                reason = f'`{missing[1] if missing else "name"}` is exported by `{exported_by}` but the snippet never imports it.'
            result.update(status=status, reason=reason, stderr=output[-8000:], exit_code=rc)
        except OSError as exc:
            if runner == 'docker':
                raise InfrastructureError(f'Docker unavailable: {exc}') from exc
            result.update(status='runtime_error', stderr=str(exc))
        finally:
            if runner == 'docker':
                try:
                    subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=10)
                except (OSError, subprocess.SubprocessError):
                    pass
                finally:
                    with _lock:
                        _active.discard(name)
    return result
