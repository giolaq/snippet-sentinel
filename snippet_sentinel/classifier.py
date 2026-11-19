import re

BROKEN = {'syntax_error', 'missing_dependency', 'runtime_error', 'timeout'}
# Statuses that say nothing about whether the docs are right; excluded from broken percentages.
INCONCLUSIVE = {'skipped', 'needs_credentials', 'needs_context'}
CAUGHT_AUTH_FAILURE = re.compile(r'AuthenticationError|Invalid API Key provided|Incorrect API key provided|'
                                 r'\b(?:statusCode|status)["\']?\s*[:=]\s*401\b|\b401 Unauthorized\b')


def undefined_name(stderr, code):
    """A JS ReferenceError for a name the snippet never declares: it continues an earlier example."""
    match = re.search(r'ReferenceError: ([\w$]+) is not defined', stderr)
    if not match:
        return None
    name = re.escape(match.group(1))
    code = re.sub(r'/\*.*?\*/|(?<![:\w])//[^\n]*', '', code, flags=re.S)
    declared = (rf'\b(?:const|let|var|class|function\s*\*?)\s+{name}\b',
                rf'\b(?:const|let|var)\s*[{{\[][^=;]*\b{name}\b',
                rf'\bimport\b[^;]*?\b{name}\b[^;]*?\bfrom\b',
                rf'\bcatch\s*\(\s*{name}\b',
                rf'\bfunction\b[^(]*\([^)]*\b{name}\b[^)]*\)',
                rf'(?:\([^()]*\b{name}\b[^()]*\)|\b{name})\s*=>')
    if any(re.search(pattern, code) for pattern in declared):
        return None
    return match.group(1)


def missing_env(stderr, code):
    names = {a or b for a, b in re.findall(r'process\.env(?:\.([A-Z][A-Z0-9_]+)|\[\s*[\'"]([A-Z][A-Z0-9_]+)[\'"]\s*\])', code)}
    return any(re.search(rf'Error\b.*\b{re.escape(n)}\b', stderr) for n in names)


def classify(stderr, exit_code, timed_out=False, install_failed=False, code=''):
    if timed_out:
        return 'timeout'
    if install_failed:
        return 'missing_dependency'
    if exit_code == 0:
        # Samples often catch and print API errors; a caught 401 did not exercise the documented call.
        return 'needs_credentials' if CAUGHT_AUTH_FAILURE.search(stderr) else 'pass'
    if re.search(r'does not provide an export named', stderr):
        return 'runtime_error'
    if re.search(r'SyntaxError|IndentationError|Unexpected token', stderr, re.I):
        return 'syntax_error'
    if re.search(r'ModuleNotFoundError|No module named|Cannot find (?:module|package)|ERR_MODULE_NOT_FOUND', stderr, re.I):
        return 'missing_dependency'
    if re.search(r'\b40[13]\b|unauthorized|invalid.?api.?key|authentication|missing credentials|missing (?:environment|env) (?:variable|var)|os\.environ|process\.env|KeyError:.*(?:KEY|TOKEN|SECRET|PASSWORD)', stderr, re.I) or missing_env(stderr, code):
        return 'needs_credentials'
    if undefined_name(stderr, code):
        return 'needs_context'
    return 'runtime_error'
