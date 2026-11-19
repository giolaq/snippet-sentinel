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
    clean = re.sub(r'''//[^\n]*|/\*[\s\S]*?\*/|'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*"|`(?:\\.|[^`\\])*`''', ' ', code)
    tokens = re.findall(r'\w+|=>|[^\s]', clean)
    scopes, parens = [], []
    pending_function = False
    arrow_expression = None
    esm = requires = top_await = False
    for i, token in enumerate(tokens):
        previous = tokens[i - 1] if i else ''
        following = tokens[i + 1] if i + 1 < len(tokens) else ''
        if token == 'import' and following not in {'(', ':'} and previous != '.':
            esm = True
        if token == 'export' and previous != '.' and following != ':':
            esm = True
        if token == 'require' and following == '(' and previous != '.':
            requires = True
        if token == 'function':
            pending_function = True
        if token == '(':
            parens.append(i)
        if token == '{':
            method = False
            if previous == ')' and parens:
                opening = parens[-1]
                before = tokens[opening - 1] if opening else ''
                method = before not in {'if', 'for', 'while', 'switch', 'catch', 'with'}
            scopes.append(pending_function or previous == '=>' or method)
            pending_function = False
        elif token == '}':
            if scopes:
                scopes.pop()
        if token == '=>' and following != '{':
            arrow_expression = (len(scopes), len(parens))
        if token in {';', ',', ')', '}'} and arrow_expression and (len(scopes), len(parens)) <= arrow_expression:
            arrow_expression = None
        if token == 'await' and previous != '.' and not any(scopes) and arrow_expression is None:
            top_await = True
        # Retain a just-closed parameter list until its following opening brace.
        if previous == ')' and parens:
            parens.pop()
    return esm or (top_await and not requires), top_await and requires and not esm


def is_esm(code):
    return javascript_mode(code)[0]


def pinned(packages, pins, lang):
    overrides = {}
    for pin in pins:
        if lang == 'python' and re.fullmatch(r'[\w.-]+==[\w.+-]+', pin):
            overrides[pin.split('==')[0].lower().replace('_', '-')] = pin
        elif lang == 'javascript' and re.fullmatch(r'(?:@[\w.-]+/)?[\w.-]+@[\w.^~+*-]+', pin):
            overrides[pin.rsplit('@', 1)[0]] = pin
    return [overrides.get(p.lower().replace('_', '-') if lang == 'python' else p, p) for p in packages]
