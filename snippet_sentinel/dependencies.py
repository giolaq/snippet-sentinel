import ast
import re
import sys

PY_PACKAGES = {'PIL': 'pillow', 'yaml': 'pyyaml', 'sklearn': 'scikit-learn',
               'cv2': 'opencv-python', 'bs4': 'beautifulsoup4', 'dotenv': 'python-dotenv',
               'dateutil': 'python-dateutil', 'jwt': 'pyjwt', 'OpenSSL': 'pyopenssl',
               'requests_oauthlib': 'requests-oauthlib', 'google': 'google-api-python-client',
               'googleapiclient': 'google-api-python-client'}
BUILTINS = set('assert async_hooks buffer child_process cluster console constants crypto dgram diagnostics_channel dns domain events fs http http2 https inspector inspector/promises module net os path perf_hooks process punycode querystring readline repl sea sqlite stream string_decoder sys test timers tls trace_events tty url util v8 vm wasi worker_threads zlib'.split())


def python_package(module):
    parts = module.split('.')
    if parts[:2] == ['google', 'cloud'] and len(parts) > 2:
        return 'google-cloud-' + parts[2].replace('_', '-')
    if parts[:2] in (['google', 'auth'], ['google', 'oauth2']):
        return 'google-auth'
    if parts[:2] == ['google', 'protobuf']:
        return 'protobuf'
    if parts[:2] == ['google', 'api_core']:
        return 'google-api-core'
    root = parts[0]
    return PY_PACKAGES.get(root, root)


def dependencies(code, lang):
    if lang == 'python':
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return []
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(n.name for n in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                if node.module == 'google.cloud':
                    names.update(node.module + '.' + n.name for n in node.names if n.name != '*')
                else:
                    names.add(node.module)
        return sorted({python_package(n) for n in names if n.split('.')[0] not in sys.stdlib_module_names})
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


def javascript_mode(code):
    return bool(re.search(r'\b(?:import|export)\s', code)) or not re.search(r'\brequire\s*\(', code)


def is_esm(code):
    return javascript_mode(code)


def pinned(packages, pins, lang):
    overrides = {}
    for pin in pins:
        if lang == 'python' and re.fullmatch(r'[\w.-]+==[\w.+-]+', pin):
            overrides[pin.split('==')[0].lower().replace('_', '-')] = pin
        elif lang == 'javascript' and re.fullmatch(r'(?:@[\w.-]+/)?[\w.-]+@[\w.^~+*-]+', pin):
            overrides[pin.rsplit('@', 1)[0]] = pin
    return [overrides.get(p.lower().replace('_', '-') if lang == 'python' else p, p) for p in packages]
