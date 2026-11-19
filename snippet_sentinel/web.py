from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .classifier import BROKEN, INCONCLUSIVE
from .scan import github_url, scan
from .storage import Store

templates = Environment(loader=FileSystemLoader(Path(__file__).parent / 'templates'), autoescape=select_autoescape())
templates.filters['urlpath'] = lambda value: quote(value, safe='/')

# Keep machine-readable verdicts in storage and URLs, not in the interface.
VERDICTS = {
    'syntax_error': ('Syntax error', 'The code could not be parsed.'),
    'missing_dependency': ('Dependency error', 'A package could not be installed or imported.'),
    'runtime_error': ('Runtime error', 'The snippet exited with an error.'),
    'timeout': ('Timed out', 'Setup or execution exceeded the time limit.'),
    'pass': ('Passed', 'The snippet completed without an error.'),
    'needs_credentials': ('Credentials needed', 'Likely requires a key or sign-in; detected heuristically, not counted as broken.'),
    'needs_context': ('Context needed', 'A fragment needs setup from an earlier example; not counted as broken.'),
    'skipped': ('Skipped', 'Not executed, for example an opted-out or incomplete example.'),
}
templates.globals.update(verdicts=VERDICTS, broken_statuses=BROKEN)


def readable_date(value):
    if not value:
        return 'Not recorded'
    try:
        date = datetime.fromisoformat(value.replace('Z', '+00:00'))
        date = date.replace(tzinfo=date.tzinfo or timezone.utc).astimezone(timezone.utc)
        return date.strftime('%b %d, %Y · %H:%M UTC')
    except ValueError:
        return 'Not recorded'


templates.filters['readable_date'] = readable_date


def present_run(run):
    """Add presentation fields without changing stored scan results."""
    run = dict(run)
    try:
        repo_url = github_url(run['repo'])
    except ValueError:
        repo_url = ''
    counts = run['counts']
    broken = sum(counts.get(name, 0) for name in BROKEN)
    excluded = sum(counts.get(name, 0) for name in INCONCLUSIVE)
    checked = sum(counts.values()) - excluded
    run.update(repo_url=repo_url,
               repo_name=repo_url.removeprefix('https://github.com/') if repo_url else
               (run['repo'].replace('\\', '/').rstrip('/').rsplit('/', 1)[-1] or 'Local folder'),
               broken=broken, runnable=checked,
               percent=100 * broken / checked if checked else None,
               excluded=excluded)
    return run


def render_run(run, static=False, status=''):
    run = present_run(run)
    # First-time visitors should reach the failures before passing/skipped samples.
    run['results'] = sorted(run['results'], key=lambda s: s['status'] not in BROKEN)
    return templates.get_template('run.html').render(run=run, static=static, status=status)


def create_app(db='sentinel.db'):
    app = FastAPI(title='Snippet Sentinel')
    store = Store(db)

    def home_page(**context):
        return templates.get_template('home.html').render(
            runs=[present_run(run) for run in store.leaderboard()], **context)

    @app.get('/', response_class=HTMLResponse)
    def home():
        return home_page()

    @app.post('/scan')
    async def start(request: Request, background: BackgroundTasks):
        form = parse_qs((await request.body()).decode())
        entered_url = form.get('url', [''])[0]
        try:
            url = github_url(entered_url)
        except ValueError as exc:
            return HTMLResponse(home_page(error=str(exc), entered_url=entered_url), status_code=400)
        run_id = store.start(url)

        def work():
            try:
                scan(url, db=db, run_id=run_id)
            except Exception:
                pass  # scan persists the error for the progress page.

        background.add_task(work)
        return RedirectResponse(f'/runs/{run_id}', status_code=303)

    @app.get('/runs/{run_id}', response_class=HTMLResponse)
    def run_page(run_id: int, status: str = ''):
        run = store.get(run_id)
        if run is None:
            raise HTTPException(404, 'Run not found')
        return render_run(run, status=status)

    return app
