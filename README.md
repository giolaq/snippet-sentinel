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

Open http://localhost:8000. The leaderboard ranks latest runs by broken percentage, excluding skipped, credential-blocked and needs-context samples. Run pages show source links pinned to commits, code, output tails, installed versions, and the first run in each consecutive failing streak. Submit a public GitHub repository on the dashboard to scan in the background; the progress page refreshes automatically.

## CLI

```sh
snippet-sentinel scan <path-or-https-GitHub-URL> \
  --ref main --lang python --runner docker --jobs 4 --timeout 60 \
  --pin requests==2.32.3 --db sentinel.db --html report.html --json out.json
snippet-sentinel scan https://github.com/axios/axios --pin axios@1.7.9
snippet-sentinel serve --db sentinel.db --port 8000 --host 127.0.0.1
```

All scan flags are optional. Repeat `--pin` for multiple packages; pins override detected packages only, otherwise the latest registry release is installed. Python pins can include extras, e.g. `--pin 'openai[aiohttp]==3.55.1'`. `--ref` selects a remote branch or tag for a shallow clone; for local paths, check out your desired revision first. `--lang` accepts `python` or `javascript`. Timeout covers setup, install and execution for each snippet. Exit status is nonzero for orchestration errors; snippet failures are recorded without failing the scan command.

Scans check at most **500 snippets** by default, after language filtering (including skipped examples). Set `--max-snippets N` to change the cap, or `--max-snippets 0` for unlimited execution. Extraction still reads all documentation; the cap bounds how many snippets are submitted to runners. CLI progress goes to **stderr**: discovered snippet/file counts, a truncation warning if applicable, and periodic completed counts. Summary stdout and `--json out.json` remain separate from progress. Reports record how many snippets were found, selected and omitted; partial-scan percentages describe only the selected examples.

Only public HTTPS GitHub repository-root URLs are supported; use `https://github.com/owner/repo`, not browser `/tree/` URLs or SSH clone syntax. Clone failures explain inaccessible/private repositories, missing branches/tags and network failures, retaining redacted Git diagnostics. Interactive Git credential prompts are disabled. The dashboard accepts an optional branch/tag, uses the same 500-snippet cap, and redirects identical URL/ref submissions to an existing queued or running scan. Completed or failed scans can be submitted again. Pending rows left by a server restart still require manual intervention; this is not a durable job queue.

When Docker is unavailable, explicitly select `--runner local`. **This executes untrusted code directly on your machine, UNSANDBOXED.** Python gets a temporary venv; JavaScript needs host Node and npm. Environment variables are not forwarded. Use only trusted samples. Missing Docker does not silently downgrade isolation.

## Extraction and results

Scans `.md`, `.mdx` and `.markdown`, excluding hidden directories, `node_modules` and `vendor`. Backtick and tilde fences, including indented fences, are supported. JS aliases (`js`, `javascript`, `jsx`, `mjs`, `cjs`, `node`) and Python aliases (`py`, `python`, `python3`) normalize automatically. TypeScript aliases (`ts`, `typescript`, `tsx`, `mts`, `cts`) normalize to `typescript`. TypeScript, and JavaScript that contains JSX, run through [tsx](https://tsx.is) (esbuild, pinned to tsx@4.23.15) instead of plain `node`, so type-only imports are elided the way `tsc` does and no type checking happens; other languages are ignored.

Opt out with a standalone `skip`, `norun` or `no-run` fence flag, a `test="skip"` or `run="skip"` attribute, or `<!-- sentinel:skip -->` on the line immediately above the fence. Lint-only exclusions do not opt out of execution. Shell-only Python fences and standalone `...` / `// ...` placeholders are skipped. In JavaScript and TypeScript, `// Before` / `// After` comparisons, Deno-only `npm:` / `jsr:` imports, method-chain fragments starting with `.`, and inline placeholders (`{ ... }`, `…`) are skipped too. A Python failure that refers to a missing name defined in an earlier fence in the same file can be skipped as a fragment; earlier code is not executed or injected. If a failing example reproduces the exception type from an immediately following `output` fence, it is skipped as a documented error. Snippets are identified by a hash of relative path, language and code, so line movement preserves history; edits or path moves start new history. Python `...` used as a value (`create(id=...)`) and Python that only fails to parse because of `...` are placeholders. Python REPL transcripts (`pycon` or `>>>`) run as the code after the prompts, ignoring expected output; transcripts that show a `Traceback` are skipped. Python snippets may use top-level `await`. Changelog files (`CHANGELOG`, `CHANGES`, `HISTORY`, `NEWS`, `RELEASES`, `RELEASE_NOTES`) are not scanned.

Statuses: `pass`, `syntax_error`, `missing_dependency`, `needs_credentials`, `needs_context`, `runtime_error`, `timeout`, `skipped`. Installation failures count as missing dependencies; credential classification is heuristic, not a guarantee. A sample that exits 0 after printing a caught authentication failure (for example a Stripe 401) is `needs_credentials`, not `pass`. A JavaScript `ReferenceError` for a name the snippet never declares (`stripeClient` set up in an earlier block, `document` in browser code) is `needs_context`. An import of a file from the reader's project (`./util`, `/components/Email`) is also `needs_context`. A missing named export (`does not provide an export named`) is a `runtime_error`, not a syntax error. A snippet that reads `process.env` and throws `Missing …` when it is unset is `needs_credentials`. A package that an SDK imports from inside `node_modules` (optional peer dependencies such as `ws` or `undici`) is installed and the snippet is retried, up to three times. JS failures caused by a `/path/to/…` placeholder file or an `example.com` host are skipped. Page-relative `fetch('/api/…')` URLs and browser-only APIs are `needs_context`. Only syntax, dependency, runtime and timeout failures count as broken. Scans with fewer than 10 checked snippets show their share marked “too few to rank” and are listed after ranked repositories, unnumbered. Incomplete, failed or zero-evaluable scans show “Not evaluated” below measured runs, not 0% broken. Broken-since streaks reset on pass, skip, credential or context status, or absence in a completed scan. See the [fairness audit](docs/fairness-audit.md) for a sample and limitations. A Python `NameError` for a name an installed package exports (`delete_repo` without its import) is reported with a missing-import reason. Hub-style `401 Client Error`, `LocalTokenNotFoundError` and “must provide an api_key” are `needs_credentials`; `ImportError: … pip install …` is `missing_dependency`. Python failures that say nothing about the docs are not counted as broken. They are `needs_context` for a value the reader supplies (`job_id=job_id`, never called or dereferenced), a local file or directory an SDK call asks the reader for, an empty Hub cache, a 404 naming a placeholder literal (`your-webhook-id`), a local server, and an optional extra the SDK raises from inside its own package. A timeout waiting for interactive login is `needs_credentials`, and a timeout while a download is still progressing is skipped. A failure whose exception the snippet names in a comment (`# Raises ValueError`) or in transcript output is skipped as documented.

Dependencies are inferred from Python AST imports (stdlib excluded, common import/package aliases mapped) and JS import/require specifiers (Node builtins excluded, scoped packages/subpaths handled). Versions are recorded after installation and before execution. Output combines stdout/stderr and keeps the last 8 KB. ESM syntax/top-level await selects `.mjs`; other JS uses `.cjs`.

## Deployment and development

```sh
docker compose up --build
pytest
```

Compose persists SQLite and mounts the Docker socket so scans can launch sibling containers. Each snippet has a 512 MB memory limit, one CPU, 256 PIDs, dropped capabilities and a timeout, and its container is removed afterwards. Images are fetched on first use; pre-pull them for reliable short timeouts.

**Security:** Docker is a practical isolation layer, not a hostile-code security boundary. Dependency installers run third-party scripts; containers have outbound network access. Docker socket access is effectively host-root access. The dashboard has no authentication, CSRF protection, scan quota or job durability: keep it on localhost or behind a trusted authenticated proxy, never expose it publicly. Use a dedicated disposable worker for untrusted repositories. Local execution has no filesystem/network isolation. Output files are bounded when retained but very noisy code can fill temporary disk while running.

## Limitations / roadmap

Samples run independently: no shared setup, relative files, interactive input or project context. Regex-based JS dependency/ESM detection can misread comments and strings; dynamic imports and Python dynamic imports may be missed. TypeScript and JSX are transpiled with esbuild via tsx, not type-checked. Python stdlib detection uses the host interpreter's module list. GitHub URLs must point to repository roots; `--ref` supports branch/tag, not arbitrary detached SHA. Identical blocks within one file share an identity. Background progress is run-level (not per-snippet); restarting the server interrupts scans. A large repo can produce many containers; `--jobs` controls concurrency. Errors may contain sensitive sample output: protect the database and reports.

Next: GitHub App integration, TypeScript type checking, more languages, durable scan queues, dependency lockfiles and stronger worker isolation.

## Verified auto-fix PRs

On a completed GitHub run, click **Propose fixes**. Offline rules handle missing stdlib/Node builtin imports, Python 2 print statements, removed collections aliases and mixed JS module modes. Each candidate runs with the same runner as the scan; only passing candidates qualify. Review the before/after diffs and verified badges, then click **Open fix PR** to publish one PR containing verified changes only. Fence bodies must still match the scanned code; mismatches are skipped and reported.

Set `GITHUB_TOKEN` (or `GH_TOKEN`) with repository contents/pull-request write permissions. Push-denied (403) repositories fall back to a fork, which requires permission to create forks. PRs target the default branch, starting from the scanned commit on `sentinel/fix-<run_id>`. No token is required to propose or verify fixes. Optional `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` enables an HTTPS LLM fallback after rules fail; this sends snippet code and error tails to the provider (Anthropic takes precedence). Without keys, proposals stay offline.

```sh
snippet-sentinel scan examples/fixable-docs --runner docker
snippet-sentinel fix <run_id> --db sentinel.db
snippet-sentinel fix <github_run_id> --open-pr
```

Fix jobs/results persist in SQLite, but background workers are not durable across restarts. Each run has one proposal job and one PR; repeat commands return stored results. Verification does not prove semantic correctness: review every PR. Local-run verification remains UNSANDBOXED. PR publication can fail after a push; inspect the remote branch before retrying.
