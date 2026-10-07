"""Repair only the exact known broken debug-master files; preserve custom edits."""
import ast
import hashlib
import os
from pathlib import Path
import tempfile

JS_HASH = 'bfdbc9307585cfe62396eaf9012041673988c8f96b457aea0adb50c90d29dcc4'
API_HASH = '2916d9ace6475bbd61695fbc0a2affb85b286b81d893ef210f52a89b5041e950'


def repaired_api(source):
    old = '''import sys
sys.path.append("/opt/data/plugins/debug-master")
from __init__ import _dump_memory, _dump_jobs, _dump_session_history, _load_state'''
    new = '''import importlib.util
from pathlib import Path
import sys
_helper_path = Path(__file__).resolve().parents[1] / "__init__.py"
_helper_spec = importlib.util.spec_from_file_location("_hermes_debug_master_helpers", _helper_path)
_helpers = importlib.util.module_from_spec(_helper_spec)
sys.modules[_helper_spec.name] = _helpers
_helper_spec.loader.exec_module(_helpers)
_dump_memory = _helpers._dump_memory
_dump_jobs = _helpers._dump_jobs
_dump_session_history = _helpers._dump_session_history
_load_state = _helpers._load_state'''
    assert old in source
    source = source.replace(old, new, 1)
    node = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == '_require_session')
    lines = source.splitlines(keepends=True)
    lines[node.lineno-1:node.end_lineno] = ['''def _require_session(request: Request) -> None:
    from hermes_cli.web_server import _has_valid_session_token
    if not _has_valid_session_token(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
''']
    result = ''.join(lines).replace('async def snapshot(', 'def snapshot(').replace('async def health(', 'def health(')
    compile(result, 'plugin_api.py', 'exec')
    return result


def write(path, payload):
    info = path.stat()
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix='.plugin-repair-')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(payload); f.flush(); os.fsync(f.fileno())
        os.chmod(temp, info.st_mode & 0o777)
        if os.geteuid() == 0:
            os.chown(temp, info.st_uid, info.st_gid)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp): os.unlink(temp)


def repair(root):
    dashboard = Path(root) / 'debug-master/dashboard'
    js = dashboard / 'bundle/index.js'
    api = dashboard / 'plugin_api.py'
    if js.is_file() and hashlib.sha256(js.read_bytes()).hexdigest() == JS_HASH:
        write(js, Path(__file__).with_name('debug-master.js').read_bytes())
        print('[dashboard] repaired debug-master JavaScript')
    if api.is_file() and hashlib.sha256(api.read_bytes()).hexdigest() == API_HASH:
        write(api, repaired_api(api.read_text()).encode())
        print('[dashboard] repaired debug-master authentication')


if __name__ == '__main__':
    import sys
    repair(sys.argv[1])
