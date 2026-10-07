"""Authenticated, bounded, read-only dashboard diagnostics."""
from datetime import datetime, timezone
import json
from pathlib import Path
from fastapi import APIRouter, Request, HTTPException

router = APIRouter()


def _require_session(request: Request) -> None:
    from hermes_cli.web_server import _has_valid_session_token
    if not _has_valid_session_token(request):
        raise HTTPException(status_code=401, detail='Unauthorized')


def _home():
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def _json(path, default):
    try:
        with path.open('rb') as f:
            raw = f.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            return default
        return json.loads(raw)
    except (OSError, ValueError):
        return default


@router.get('/snapshot')
def snapshot(request: Request):
    _require_session(request)
    home = _home()
    memory = {}
    for name in ('MEMORY.md', 'USER.md'):
        for path in (home/'memories'/name, home/name):
            try:
                with path.open('r', encoding='utf-8', errors='replace') as f:
                    memory[name] = f.read(16000)
                break
            except OSError:
                pass
    raw_jobs = _json(home/'cron/jobs.json', [])
    if isinstance(raw_jobs, dict):
        raw_jobs = raw_jobs.get('jobs', [])
    jobs = [{k:j.get(k) for k in ('id','job_id','name','enabled','next_run_at','last_run_at')}
            for j in raw_jobs[:20] if isinstance(j, dict)] if isinstance(raw_jobs, list) else []
    history = []
    history_error = None
    try:
        from _hermes_dashboard_runtime import AnalyticsDB
        db = AnalyticsDB(home/'state.db')
        try:
            available = {r['name'] for r in db._conn.execute('PRAGMA table_info(sessions)')}
            columns = [k for k in ('id','title','model','source','started_at') if k in available]
            history = [dict(r) for r in db._conn.execute(
                'SELECT '+','.join(columns)+' FROM sessions ORDER BY started_at DESC LIMIT 20')]
        finally:
            db.close()
    except Exception:
        history_error = 'Session database is not ready for reading.'
    raw_state = _json(home/'state.json', {})
    state = {k:raw_state[k] for k in ('balance','points','credits','coins') if k in raw_state} if isinstance(raw_state, dict) else {}
    return {'memory':memory, 'jobs':jobs, 'history_preview':history,
            'history_error':history_error, 'runtime_state':state,
            'timestamp':datetime.now(timezone.utc).isoformat()}


@router.get('/health')
def health(request: Request):
    _require_session(request)
    return {'status':'ok'}
