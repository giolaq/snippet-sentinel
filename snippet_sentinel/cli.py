import argparse
import json
from pathlib import Path

from .scan import scan
from .web import create_app, render_run


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError('must be positive')
    return number


def main():
    parser = argparse.ArgumentParser(description='Test documentation snippets. Local runner is UNSANDBOXED.')
    commands = parser.add_subparsers(dest='command', required=True)
    scanner = commands.add_parser('scan')
    scanner.add_argument('source')
    scanner.add_argument('--ref')
    scanner.add_argument('--lang', choices=['python', 'javascript'])
    scanner.add_argument('--runner', choices=['docker', 'local'], default='docker')
    scanner.add_argument('--jobs', type=positive, default=4)
    scanner.add_argument('--timeout', type=positive, default=60)
    scanner.add_argument('--pin', action='append', default=[])
    scanner.add_argument('--db', default='sentinel.db')
    scanner.add_argument('--html')
    scanner.add_argument('--json')
    server = commands.add_parser('serve')
    server.add_argument('--port', type=positive, default=8000)
    server.add_argument('--db', default='sentinel.db')
    server.add_argument('--host', default='127.0.0.1')
    args = parser.parse_args()
    if args.command == 'serve':
        import uvicorn
        uvicorn.run(create_app(args.db), host=args.host, port=args.port)
        return
    if args.runner == 'local':
        print('WARNING: local runner is UNSANDBOXED. Only scan trusted code.')
    try:
        result = scan(args.source, args.db, args.ref, args.lang, args.runner, args.jobs, args.timeout, args.pin)
    except Exception as exc:
        parser.exit(1, f'Scan failed: {exc}\n')
    print(f"Run {result['id']} | {result['repo']} | {result['ref']} | runner={args.runner}")
    print(f"{'STATUS':<24} COUNT")
    for status, count in sorted(result['counts'].items()):
        print(f'{status:<24} {count}')
    print(f"unsupported (TypeScript) {result['stats'].get('unsupported', 0)}")
    if args.html:
        Path(args.html).write_text(render_run(result, static=True))
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2))
