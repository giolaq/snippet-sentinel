import ast
import doctest
import hashlib
import os
import re
import symtable
from dataclasses import asdict, dataclass
from pathlib import Path

from .classifier import final_error

LANGS = {**dict.fromkeys(('js', 'javascript', 'jsx', 'mjs', 'cjs', 'node'), 'javascript'),
         **dict.fromkeys(('py', 'python', 'python3', 'pycon'), 'python')}
SKIP_DIRS = {'.git', 'node_modules', 'vendor'}
OPT_OUT = re.compile(r"""(?<!\S)(?:skip|norun|no-run)(?!\S)|(?:^|[\s{])(?:test|run)\s*=\s*(?:"skip"|'skip'|skip(?=[\s}]|$))""", re.I)
SHELL_LINE = re.compile(r'^\s*(?:\$\s+\S|!?(?:pip3?|python3?\s+-m\s+pip)\s+(?:install|uninstall|show|freeze|list)\b|export\s+[A-Za-z_]\w*=)')


@dataclass
class Snippet:
    id: str
    file: str
    line: int
    lang: str
    code: str
    reason: str = ''
    context_names: tuple[str, ...] = ()
    expected_error: str = ''

    def dict(self):
        data = asdict(self)
        del data['context_names'], data['expected_error']
        return data


def python_scope(code, transcript):
    try:
        if transcript:
            code = '\n'.join(example.source for example in doctest.DocTestParser().get_examples(code))
        return symtable.symtable(code, '<snippet>', 'exec')
    except (SyntaxError, ValueError):
        return None


def expected_error(lines, end):
    while end < len(lines) and not lines[end].strip():
        end += 1
    if end == len(lines):
        return ''
    fence = re.fullmatch(r'\s*([\x60]{3,}|~{3,})output\s*', lines[end])
    if not fence:
        return ''
    marker = fence[1]
    output = []
    for line in lines[end + 1:]:
        if re.fullmatch(r'\s*' + re.escape(marker[0]) + '{' + str(len(marker)) + r',}\s*', line):
            break
        output.append(line)
    error = final_error('\n'.join(output))
    return error['name'] if error else ''


def extract(root):
    snippets, stats = [], {'unsupported': 0}
    root = Path(root)
    for directory, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not Path(directory, d).is_symlink())
        for name in sorted(files):
            path = Path(directory, name)
            if path.suffix.lower() not in {'.md', '.mdx', '.markdown'} or path.is_symlink():
                continue
            lines = path.read_text(errors='replace').splitlines()
            python_bindings = set()
            i = 0
            while i < len(lines):
                fence = re.match(r'^\s*(`{3,}|~{3,})(.*)$', lines[i])
                if not fence:
                    i += 1
                    continue
                marker, info = fence.groups()
                indent = len(lines[i]) - len(lines[i].lstrip())
                start = i
                i += 1
                body = []
                while i < len(lines) and not re.match(r'^\s*' + re.escape(marker[0]) + '{' + str(len(marker)) + r',}\s*$', lines[i]):
                    line = lines[i]
                    remove = min(indent, len(line) - len(line.lstrip()))
                    body.append(line[remove:])
                    i += 1
                i += 1
                raw_lang = info.strip().split()[0].lower() if info.strip() else ''
                lang = LANGS.get(raw_lang)
                if not lang:
                    if raw_lang in {'ts', 'typescript', 'tsx'}:
                        stats['unsupported'] += 1
                    continue
                code = '\n'.join(body)
                transcript = raw_lang == 'pycon' or code.lstrip().startswith('>>> ')
                shell = lang == 'python' and bool(code.strip()) and all(
                    SHELL_LINE.match(line) for line in body if line.strip() and not line.lstrip().startswith('#')
                ) and any(SHELL_LINE.match(line) for line in body)
                scope = python_scope(code, transcript) if lang == 'python' and not shell else None
                context_names = ()
                if scope and not transcript:
                    context_names = tuple(sorted(symbol.get_name() for symbol in scope.get_symbols()
                                                 if symbol.get_name() in python_bindings and symbol.is_referenced()
                                                 and not symbol.is_assigned() and not symbol.is_imported()))
                reason = ''
                if OPT_OUT.search(info.strip()[len(raw_lang):]):
                    reason = 'info-string opt-out'
                elif start and lines[start - 1].strip() == '<!-- sentinel:skip -->':
                    reason = 'sentinel:skip comment'
                elif transcript:
                    reason = 'REPL transcript'
                elif shell:
                    reason = 'shell commands in a Python fence'
                elif any(re.match(r'^\s*(?://\s*)?\.\.\.\s*$', line) for line in body):
                    if lang != 'python' or not scope or any(
                        isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and node.value.value is Ellipsis
                        and body[node.lineno - 1].strip() == '...' for node in ast.walk(ast.parse(code))
                    ):
                        reason = 'placeholder ellipsis'
                if scope and (not reason or transcript):
                    python_bindings.update(symbol.get_name() for symbol in scope.get_symbols()
                                           if symbol.is_assigned() or symbol.is_imported() or symbol.is_namespace())
                relative = path.relative_to(root).as_posix()
                identity = hashlib.sha256((relative + '\0' + lang + '\0' + code).encode()).hexdigest()[:24]
                snippets.append(Snippet(identity, relative, start + 2, lang, code, reason,
                                        context_names, expected_error(lines, i)))
    return snippets, stats
