import re

BROKEN = {'syntax_error', 'missing_dependency', 'runtime_error', 'timeout'}


def classify(stderr, exit_code, timed_out=False, install_failed=False):
    if timed_out:
        return 'timeout'
    if install_failed:
        return 'missing_dependency'
    if exit_code == 0:
        return 'pass'
    if re.search(r'SyntaxError|IndentationError|Unexpected token', stderr, re.I):
        return 'syntax_error'
    if re.search(r'ModuleNotFoundError|No module named|Cannot find (?:module|package)|ERR_MODULE_NOT_FOUND', stderr, re.I):
        return 'missing_dependency'
    if re.search(r'\b40[13]\b|unauthorized|invalid.?api.?key|authentication|missing (?:environment|env) (?:variable|var)|os\.environ|process\.env|KeyError:.*(?:KEY|TOKEN|SECRET|PASSWORD)', stderr, re.I):
        return 'needs_credentials'
    return 'runtime_error'
