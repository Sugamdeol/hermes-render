"""CI-only load fixtures; no real Telegram token or GitHub credentials."""
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

root = Path('/tmp/hermes-combined-probe')
root.mkdir(exist_ok=True)
stop = threading.Event()
signal.signal(signal.SIGTERM, lambda *_: stop.set())
signal.signal(signal.SIGINT, lambda *_: stop.set())

if sys.argv[1] == 'gateway':
    from gateway.run import GatewayRunner
    runner = GatewayRunner()
    (root / 'gateway-ready').touch()
    stop.wait()
else:
    spec = importlib.util.spec_from_file_location('git_storage', '/opt/render-tools/git-storage.py')
    storage = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(storage)
    remote = root / 'remote.git'
    data = root / 'data'
    data.mkdir(exist_ok=True)
    subprocess.run(['git', 'init', '-q', '--bare', str(remote)], check=True)
    # Fixture routing only; production private-repository checks stay intact.
    storage.GitConfig.remote_url = property(lambda self: str(remote))
    storage.GitConfig.api_repo = property(lambda self: 'local/ci-fixture')
    storage.repo_is_private = lambda config: True
    config = storage.GitConfig(repo='local/ci-fixture', token='ci-only',
        workdir=root / 'work', seed_on_boot=True, env_mode='encrypt',
        watch_seconds=1, debounce_seconds=1, min_push_interval=1, interval=3)
    for index in range(1724):
        (data / ('memory-%04d.md' % index)).write_bytes(os.urandom(4096))
    with (data / 'sessions.bin').open('wb') as handle:
        for _ in range(16):
            handle.write(os.urandom(1048576))
    (data / '.env').write_text('CI_ONLY_SECRET=fixture-value\n')
    storage.seed(data, config)
    (root / 'storage-ready').touch()
    def changes():
        while not stop.wait(2):
            (data / 'latest-memory.md').write_text(str(time.time()))
    threading.Thread(target=changes, daemon=True).start()
    storage.run_daemon(data, config, stop=stop)
