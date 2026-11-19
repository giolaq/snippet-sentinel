import hashlib
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path

LANGS = {**dict.fromkeys(('js', 'javascript', 'jsx', 'mjs', 'cjs', 'node'), 'javascript'),
         **dict.fromkeys(('py', 'python', 'python3'), 'python')}
SKIP_DIRS = {'.git', 'node_modules', 'vendor'}


@dataclass
class Snippet:
    id: str
    file: str
    line: int
    lang: str
    code: str
    reason: str = ''

    def dict(self):
        return asdict(self)


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
                reason = ''
                if re.search(r'\b(skip|norun|no-run)\b', info, re.I):
                    reason = 'info-string opt-out'
                elif start and lines[start - 1].strip() == '<!-- sentinel:skip -->':
                    reason = 'sentinel:skip comment'
                elif any(re.match(r'^\s*>>>', line) for line in body):
                    reason = 'REPL transcript'
                elif any(re.match(r'^\s*(?://\s*)?\.\.\.\s*$', line) for line in body):
                    reason = 'placeholder ellipsis'
                relative = path.relative_to(root).as_posix()
                identity = hashlib.sha256((relative + '\0' + lang + '\0' + code).encode()).hexdigest()[:24]
                snippets.append(Snippet(identity, relative, start + 2, lang, code, reason))
    return snippets, stats
