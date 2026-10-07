"""Repair only the exact known broken debug-master files; preserve custom edits."""
import hashlib
import os
from pathlib import Path
import tempfile

JS_HASH = 'bfdbc9307585cfe62396eaf9012041673988c8f96b457aea0adb50c90d29dcc4'
API_HASH = '2916d9ace6475bbd61695fbc0a2affb85b286b81d893ef210f52a89b5041e950'

API_PREVIOUS_HASH = 'a9d4eaaacf3664ef8f72b3618ad098cd0c71af12a375289ed2b8baf6e5142ea5'

def repaired_api(source):
    # The generated helper also crashes during import. The dashboard does not
    # need agent hooks or tools to inspect a bounded read-only snapshot.
    result = Path(__file__).with_name('debug-master-api.py').read_text()
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
    if api.is_file() and hashlib.sha256(api.read_bytes()).hexdigest() in (API_HASH, API_PREVIOUS_HASH):
        write(api, repaired_api(api.read_text()).encode())
        print('[dashboard] repaired debug-master authentication')


if __name__ == '__main__':
    import sys
    repair(sys.argv[1])
