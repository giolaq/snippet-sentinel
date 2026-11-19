import re

BROKEN = {'syntax_error', 'missing_dependency', 'runtime_error', 'timeout', 'output_limit', 'disk_limit'}
# Statuses that say nothing about whether the docs are right; excluded from broken percentages.
INCONCLUSIVE = {'skipped', 'needs_credentials', 'needs_context', 'needs_network'}
CAUGHT_AUTH_FAILURE = re.compile(r'AuthenticationError|Invalid API Key provided|Incorrect API key provided|'
                                 r'\b(?:statusCode|status)["\']?\s*[:=]\s*401\b|\b401 Unauthorized\b')
ERROR_LINE = re.compile(
    r'^(?:\w+\.)*(?P<name>\w*(?:Error|Exception))(?: \[(?P<code>[A-Z_]+)\])?'
    r'(?::[ \t]*(?P<message>[^\n]*))?$',
    re.M,
)


def final_error(output):
    return next(reversed(list(ERROR_LINE.finditer(output))), None)


def error_summary(output):
    error = final_error(output)
    if error:
        return error[0]
    output = output.strip()
    return output if '\n' not in output else ''


def undefined_name(stderr, code):
    """A JS ReferenceError for a name the snippet never declares: it continues an earlier example."""
    match = re.search(r'ReferenceError: ([\w$]+) is not defined(?! in ES module scope)', stderr)
    if not match:
        return None
    name = re.escape(match.group(1))
    code = re.sub(r'''('|"|`)(?:\\.|(?!\1)[^\\])*\1|/\*.*?\*/|//[^\n]*''',
                  lambda m: '""' if m.group(1) else '', code, flags=re.S)
    declared = (rf'\b(?:const|let|var|class|function\s*\*?)\s+{name}\b',
                rf'\b(?:const|let|var)\s*[{{\[][^=;\]}}]*\b{name}\b',
                rf'\bimport\b[^;]*?\b{name}\b[^;]*?\bfrom\b',
                rf'\bcatch\s*\(\s*{name}\b',
                rf'\bfunction\b[^(]*\([^)]*\b{name}\b[^)]*\)',
                rf'(?:\([^()]*\b{name}\b[^()]*\)|\b{name})\s*=>')
    if any(re.search(pattern, code) for pattern in declared):
        return None
    return match.group(1)


def missing_env(stderr, code):
    names = {a or b for a, b, _ in re.findall(
        r'process\.env(?:\.([A-Z][A-Z0-9_]+)(?![\w$])|\[\s*[\'"]([A-Z][A-Z0-9_]+)[\'"]\s*\])(?!\s*(?:\?\?|\|\|)(?!\s*([\'"\x60])\3))', code)}
    unset = r'Error: Missing\b|Cannot read properties of undefined|\b(?:is )?(?:mandatory|required)\b'
    return bool(names) and (bool(re.search(unset, stderr)) or
                            any(re.search(rf'Error\b.*\b{re.escape(n)}\b', stderr) for n in names))


def local_import(stderr):
    """A relative or absolute file import: the snippet expects a file from the reader's own project."""
    match = re.search(r"Cannot find module '((?:\.{1,2}/|/)(?![^']*/node_modules/)[^']+)' imported from \S*snippet\.", stderr)
    return match and match.group(1)


def browser_only(stderr):
    """Browser APIs or page-relative URLs: the example is meant to run in a web page, not Node."""
    match = re.search(r'Failed to parse URL from (/\S*)|\brequires a browser\b', stderr)
    return match and (f'Fetches the page-relative URL `{match.group(1)}`' if match.group(1) else 'Needs a browser')


def context_reason(output, code):
    stderr = error_summary(output)
    if name := undefined_name(stderr, code):
        return f'Uses `{name}` without defining it: likely continues an earlier example or expects a browser page.'
    if path := local_import(stderr):
        return f'Imports `{path}` from the reader\'s own project.'
    if browser := browser_only(stderr if final_error(output) else output):
        return f'{browser}: meant to run in a web page, not Node.'
    return None


def classify(stderr, exit_code, timed_out=False, install_failed=False, code='', network_disabled=False):
    if 'SENTINEL_OUTPUT_LIMIT:' in stderr:
        return 'output_limit'
    if timed_out:
        return 'timeout'
    if exit_code != 0 and re.search(r'No space left on device|ENOSPC|Read-only file system|EROFS', stderr, re.I):
        return 'disk_limit'
    if install_failed:
        return 'missing_dependency'
    if exit_code == 0:
        # Samples often catch and print API errors; a caught 401 did not exercise the documented call.
        return 'needs_credentials' if CAUGHT_AUTH_FAILURE.search(stderr) else 'pass'
    summary = error_summary(stderr)
    if re.search(r'does not provide an export named', summary):
        return 'runtime_error'
    if network_disabled and re.search(r'Network is unreachable|ENETUNREACH|EHOSTUNREACH|EAI_AGAIN|ENOTFOUND|Temporary failure in name resolution|Name or service not known', summary, re.I):
        return 'needs_network'
    if (re.search(r'^(?:SyntaxError|IndentationError|TabError)\b|Unexpected token|Transform failed', summary, re.I)
            and not re.search(r'in JSON\b|is not valid JSON|Invalid JSON', summary)):
        return 'syntax_error'
    if local_import(summary):
        return 'needs_context'
    if re.search(r'ModuleNotFoundError|No module named|Cannot find (?:module|package)|ERR_MODULE_NOT_FOUND'
                 r'|^ImportError:.*\bpip install\b', summary, re.I):
        return 'missing_dependency'
    if re.search(r'\b(?:HTTP(?: status)?|status(?: code)?|client error)\s*:?\s*[\'\"]?40[13]\b|\b40[13]\s+(?:Unauthorized|Forbidden)\b'
                 r'|\b(?:Missing|No|Invalid|Expired) (?:\w+ )?(?:credentials?|API[ _-]?key|access token)\b'
                 r'|\b(?:AuthenticationError|UnauthorizedError|LocalTokenNotFoundError)\b|failed to read (?:the token file|[\w ]*\btoken from)\b'
                 r'|\bCould not find credentials\b|\bcredentials? requires\b|\bauthentication failed\b'
                 r'|\b40[13] Client Error\b|\b[Tt]oken is required\b|\bmust provide an api_key\b|Could not resolve authentication method'
                 r'|User authentication is required'
                 r'|(?:KeyError:\s*[\'\"]|\b(?:os\.environ|process\.env)\.)[A-Z_]*(?:KEY|TOKEN|SECRET|PASSWORD|CERTIFICATE)[A-Z_]*\b'
                 r'|missing (?:environment|env) (?:variable|var)\s+[\x60\'\"]?[A-Z_]*(?:KEY|TOKEN|SECRET|PASSWORD|CERTIFICATE)[A-Z_]*\b'
                 r'|[\x60\'\"][A-Z][A-Z0-9_]+[\x60\'\"] environment variable\b', summary, re.I) or missing_env(summary, code):
        return 'needs_credentials'
    if undefined_name(summary, code) or browser_only(summary if final_error(stderr) else stderr):
        return 'needs_context'
    return 'runtime_error'
