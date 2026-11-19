import json
import sqlite3
from datetime import datetime, timezone

from .classifier import BROKEN, INCONCLUSIVE


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path='sentinel.db'):
        self.path = str(path)
        with self.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY, repo TEXT, ref TEXT, started TEXT, finished TEXT,
                counts TEXT, stats TEXT, error TEXT);
                CREATE TABLE IF NOT EXISTS results (
                id INTEGER PRIMARY KEY, run_id INTEGER REFERENCES runs(id), snippet_id TEXT, data TEXT);
                CREATE INDEX IF NOT EXISTS results_run ON results(run_id);''')

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def start(self, repo, ref=''):
        with self.connect() as db:
            return db.execute('INSERT INTO runs(repo,ref,started) VALUES(?,?,?)', (repo, ref, now())).lastrowid

    def finish(self, run_id, results, stats, ref='', error=''):
        counts = {}
        for result in results:
            counts[result['status']] = counts.get(result['status'], 0) + 1
        with self.connect() as db:
            for result in results:
                db.execute('INSERT INTO results(run_id,snippet_id,data) VALUES(?,?,?)',
                           (run_id, result['id'], json.dumps(result)))
            db.execute('UPDATE runs SET finished=?,counts=?,stats=?,ref=?,error=? WHERE id=?',
                       (now(), json.dumps(counts), json.dumps(stats), ref, error, run_id))

    def get(self, run_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
            if not row:
                return None
            run = dict(row)
            run['counts'] = json.loads(run['counts'] or '{}')
            run['stats'] = json.loads(run['stats'] or '{}')
            results = [json.loads(r['data']) for r in db.execute('SELECT data FROM results WHERE run_id=? ORDER BY id', (run_id,))]
            for result in results:
                result['broken_since'] = None
                if result['status'] not in BROKEN:
                    continue
                history = db.execute('''SELECT r.id,r.started,x.data FROM runs r
                    LEFT JOIN results x ON x.run_id=r.id AND x.snippet_id=?
                    WHERE r.repo=? AND r.id<=? AND r.finished IS NOT NULL ORDER BY r.id DESC''',
                    (result['id'], run['repo'], run_id)).fetchall()
                for old in history:
                    data = json.loads(old['data']) if old['data'] else None
                    if not data or data['status'] not in BROKEN:
                        break
                    result['broken_since'] = {'run_id': old['id'], 'started': old['started'], 'versions': data['versions']}
            run['results'] = results
            return run

    def leaderboard(self):
        with self.connect() as db:
            rows = db.execute('SELECT id FROM runs WHERE id IN (SELECT MAX(id) FROM runs GROUP BY repo)').fetchall()
        board = []
        for row in rows:
            run = self.get(row['id'])
            c = run['counts']
            broken = sum(c.get(s, 0) for s in BROKEN)
            runnable = sum(c.values()) - sum(c.get(s, 0) for s in INCONCLUSIVE)
            percent = 100 * broken / runnable if run['finished'] and not run['error'] and runnable else None
            run.update(broken=broken, runnable=runnable, percent=percent)
            board.append(run)
        return sorted(board, key=lambda r: r['percent'] if r['percent'] is not None else -1, reverse=True)
