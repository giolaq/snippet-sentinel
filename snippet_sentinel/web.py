from pathlib import Path
from urllib.parse import parse_qs, quote

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .scan import github_url, scan
from .storage import Store

templates = Environment(loader=FileSystemLoader(Path(__file__).parent / 'templates'), autoescape=select_autoescape())
templates.filters['urlpath'] = lambda value: quote(value, safe='/')


def render_run(run, static=False, status=''):
    return templates.get_template('run.html').render(run=run, static=static, status=status)


def create_app(db='sentinel.db'):
    app = FastAPI(title='Snippet Sentinel')
    store = Store(db)

    @app.get('/', response_class=HTMLResponse)
    def home():
        return templates.get_template('home.html').render(runs=store.leaderboard())

    @app.post('/scan')
    async def start(request: Request, background: BackgroundTasks):
        form = parse_qs((await request.body()).decode())
        try:
            url = github_url(form.get('url', [''])[0])
        except ValueError as exc:
            raise HTTPException(400, str(exc))
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
