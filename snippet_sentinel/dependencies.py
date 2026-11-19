import ast
import re
import sys

PY_PACKAGES = {'PIL': 'pillow', 'yaml': 'pyyaml', 'sklearn': 'scikit-learn',
               'cv2': 'opencv-python', 'bs4': 'beautifulsoup4', 'dotenv': 'python-dotenv',
               'dateutil': 'python-dateutil', 'jwt': 'pyjwt', 'OpenSSL': 'pyopenssl',
               'requests_oauthlib': 'requests-oauthlib', 'google': 'google-api-python-client'}
BUILTINS = set('assert async_hooks buffer child_process cluster console constants crypto dgram diagnostics_channel dns domain events fs http http2 https inspector inspector/promises module net os path perf_hooks process punycode querystring readline repl sea sqlite stream string_decoder sys test timers tls trace_events tty url util v8 vm wasi worker_threads zlib'.split())


def dependencies(code, lang):
    if lang == 'python':
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return []
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(n.name.split('.')[0] for n in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names.add(node.module.split('.')[0])
        return sorted({PY_PACKAGES.get(n, n) for n in names if n not in sys.stdlib_module_names})
    specs = re.findall(r'''(?:\bfrom\s*|\bimport\s*(?:\(\s*)?|\brequire\s*\(\s*)['"]([^'"]+)['"]''', code)
    packages = set()
    for spec in specs:
        if spec.startswith(('.', '/', '#', 'node:', 'http:', 'https:', 'data:')):
            continue
        if spec.split('/')[0] in BUILTINS:
            continue
        package = '/'.join(spec.split('/')[:2]) if spec.startswith('@') else spec.split('/')[0]
        if re.fullmatch(r'(?:@[\w.-]+/)?[\w.-]+', package):
            packages.add(package)
    return sorted(packages)


def is_esm(code):
    return bool(re.search(r'\b(?:import|export)\s|\bimport\s*\(|\bawait\b', code))


def pinned(packages, pins, lang):
    overrides = {}
    for pin in pins:
        if lang == 'python' and re.fullmatch(r'[\w.-]+==[\w.+-]+', pin):
            overrides[pin.split('==')[0].lower().replace('_', '-')] = pin
        elif lang == 'javascript' and re.fullmatch(r'(?:@[\w.-]+/)?[\w.-]+@[\w.^~+*-]+', pin):
            overrides[pin.rsplit('@', 1)[0]] = pin
    return [overrides.get(p.lower().replace('_', '-') if lang == 'python' else p, p) for p in packages]
