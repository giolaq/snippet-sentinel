# Snippet Sentinel

**Find broken code samples before your readers do.** Snippet Sentinel extracts JavaScript and Python fenced blocks from your documentation, checks them against fresh dependencies, and gives DevRel teams a dashboard of what broke and when.

## Quickstart

Requires Python 3.10+, Git and a running Docker daemon. JavaScript runs in `node:22-slim`; Python runs in `python:3.12-slim`. No host Node installation is required for Docker scans.

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
snippet-sentinel scan examples/demo-docs --runner docker
snippet-sentinel scan https://github.com/psf/requests --jobs 4
snippet-sentinel serve --port 8000
```

Open http://localhost:8000. The leaderboard ranks latest runs by broken percentage, excluding skipped and credential-blocked samples. Run pages show source links pinned to commits, code, output tails, installed versions, and the first run in each consecutive failing streak. Submit a public GitHub repository on the dashboard to scan in the background; the progress page refreshes automatically.

## CLI

```sh
snippet-sentinel scan <path-or-https-GitHub-URL> \
  --ref main --lang python --runner docker --jobs 4 --timeout 60 \
  --pin requests==2.32.3 --db sentinel.db --html report.html --json out.json
snippet-sentinel scan https://github.com/axios/axios --pin axios@1.7.9
snippet-sentinel serve --db sentinel.db --port 8000 --host 127.0.0.1
```

All scan flags are optional. Repeat `--pin` for multiple packages; pins override detected packages only, otherwise the latest registry release is installed. `--ref` selects a remote branch or tag for a shallow clone; for local paths, check out your desired revision first. `--lang` accepts `python` or `javascript`. Timeout covers setup, install and execution for each snippet. Exit status is nonzero for orchestration errors; snippet failures are recorded without failing the scan command.

When Docker is unavailable, explicitly select `--runner local`. **This executes untrusted code directly on your machine, UNSANDBOXED.** Python gets a temporary venv; JavaScript needs host Node and npm. Environment variables are not forwarded. Use only trusted samples. Missing Docker does not silently downgrade isolation.

## Extraction and results

Scans `.md`, `.mdx` and `.markdown`, excluding `.git`, `node_modules` and `vendor`. Backtick and tilde fences, including indented fences, are supported. JS aliases (`js`, `javascript`, `jsx`, `mjs`, `cjs`, `node`) and Python aliases (`py`, `python`, `python3`) normalize automatically. TypeScript is counted as unsupported; other languages are ignored.

Opt out with `skip`, `norun` or `no-run` in the fence info string, or `<!-- sentinel:skip -->` on the line immediately above it. REPL transcripts (`>>>`) and standalone `...` / `// ...` placeholders are skipped. Snippets are identified by a hash of relative path and code, so line movement preserves history; edits or path moves start new history.

Statuses: `pass`, `syntax_error`, `missing_dependency`, `needs_credentials`, `runtime_error`, `timeout`, `skipped`. Installation failures count as missing dependencies; credential classification is heuristic, not a guarantee. Only syntax, dependency, runtime and timeout failures count as broken. Empty/credential-only repositories show 0% with a zero evaluable denominator. Broken-since streaks reset on pass, skip, credential status or absence in a completed scan.

Dependencies are inferred from Python AST imports (stdlib excluded, common import/package aliases mapped) and JS import/require specifiers (Node builtins excluded, scoped packages/subpaths handled). Versions are recorded after installation and before execution. Output combines stdout/stderr and keeps the last 8 KB. ESM syntax/top-level await selects `.mjs`; other JS uses `.cjs`.

## Deployment and development

```sh
docker compose up --build
pytest
```

Compose persists SQLite and mounts the Docker socket so scans can launch sibling containers. Each snippet has a 512 MB memory limit, one CPU, 256 PIDs, dropped capabilities and a timeout, and its container is removed afterwards. Images are fetched on first use; pre-pull them for reliable short timeouts.

**Security:** Docker is a practical isolation layer, not a hostile-code security boundary. Dependency installers run third-party scripts; containers have outbound network access. Docker socket access is effectively host-root access. The dashboard has no authentication, CSRF protection, scan quota or job durability: keep it on localhost or behind a trusted authenticated proxy, never expose it publicly. Use a dedicated disposable worker for untrusted repositories. Local execution has no filesystem/network isolation. Output files are bounded when retained but very noisy code can fill temporary disk while running.

## Limitations / roadmap

Samples run independently: no shared setup, relative files, interactive input or project context. Regex-based JS dependency/ESM detection can misread comments and strings; dynamic imports and Python dynamic imports may be missed. JSX aliases are recognized but no transpilation is provided. Python stdlib detection uses the host interpreter's module list. GitHub URLs must point to repository roots; `--ref` supports branch/tag, not arbitrary detached SHA. Identical blocks within one file share an identity. Background progress is run-level (not per-snippet); restarting the server interrupts scans. A large repo can produce many containers; `--jobs` controls concurrency. Errors may contain sensitive sample output: protect the database and reports.

Next: GitHub App integration, suggested fixes / auto-fix PRs, TypeScript, more languages, durable scan queues, dependency lockfiles and stronger worker isolation. **GitHub App and auto-fix PRs are not part of v1.**
