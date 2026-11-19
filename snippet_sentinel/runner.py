import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from .classifier import classify
from .dependencies import dependencies, is_esm, pinned


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
    filename = 'snippet.py' if python else ('snippet.mjs' if is_esm(snippet.code) else 'snippet.cjs')
    name = 'sentinel-' + uuid.uuid4().hex
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='sentinel-') as directory:
        path = Path(directory)
        path.joinpath(filename).write_text(snippet.code)
        # A tiny worker keeps package discovery and execution identical across backends.
        worker = '''import json, subprocess, sys
packages, specs, python, filename = json.loads(sys.argv[1])
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
        payload = json.dumps([packages, specs, python, filename])
        try:
            if runner == 'docker':
                # Node images have no Python, so use a native JS worker there.
                if not python:
                    js_worker = '''const {spawnSync} = require('node:child_process');
const [packages,specs,,filename] = JSON.parse(process.argv[2]);
if (specs.length) {
 const p = spawnSync('npm', ['install','--no-audit','--no-fund','--',...specs], {encoding:'utf8'});
 if(p.status !== 0) { console.log('SENTINEL_INSTALL_FAILED'); console.log(p.stdout, p.stderr); process.exit(p.status || 1); }
}
const p = spawnSync('npm',['ls','--json','--depth=0'],{encoding:'utf8'});
const deps = JSON.parse(p.stdout || '{}').dependencies || {};
console.log('SENTINEL_VERSIONS='+JSON.stringify(Object.fromEntries(packages.filter(x=>deps[x]).map(x=>[x,deps[x].version]))));
const r = spawnSync('node',[filename],{stdio:'inherit'}); process.exit(r.status === null ? 1 : r.status);
'''
                    path.joinpath('worker.cjs').write_text(js_worker)
                # Copy instead of bind mounting: works with the dashboard's socket mount.
                image = 'python:3.12-slim' if python else 'node:22-slim'
                out, rc, expired = command(['docker', 'create', '--name', name, '--memory', '512m', '--cpus', '1',
                    '--pids-limit', '256', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                    '--workdir', '/work', image, 'python' if python else 'node',
                    'worker.py' if python else 'worker.cjs', payload], directory, timeout)
                if rc or expired:
                    return {**result, 'status': 'timeout' if expired else 'runtime_error', 'stderr': out, 'exit_code': rc}
                out, rc, expired = command(['docker', 'cp', directory + '/.', name + ':/work'], directory, timeout - (time.monotonic() - started))
                if not rc and not expired:
                    out, rc, expired = command(['docker', 'start', '-a', name], directory, timeout - (time.monotonic() - started))
                    if not expired:
                        inspect = subprocess.run(['docker', 'inspect', '-f', '{{.State.ExitCode}}', name], capture_output=True, text=True)
                        if inspect.returncode == 0:
                            rc = int(inspect.stdout.strip())
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
                out, rc, expired = command([executable, 'worker.py', payload], directory, timeout - (time.monotonic() - started), env)
            lines = []
            for line in out.splitlines():
                if line.startswith('SENTINEL_VERSIONS='):
                    try:
                        result['versions'] = json.loads(line.split('=', 1)[1])
                    except ValueError:
                        pass
                else:
                    lines.append(line)
            result.update(status=classify(out, rc, expired, 'SENTINEL_INSTALL_FAILED' in out),
                          stderr='\n'.join(lines)[-8000:], exit_code=rc)
        except OSError as exc:
            result.update(status='runtime_error', stderr=str(exc))
        finally:
            if runner == 'docker':
                try:
                    subprocess.run(['docker', 'rm', '-f', name], capture_output=True)
                except OSError:
                    pass
    return result
