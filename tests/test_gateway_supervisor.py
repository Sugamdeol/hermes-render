import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest


SCRIPT = Path(__file__).parents[1] / 'scripts/gateway-supervisor.sh'


class GatewaySupervisorTests(unittest.TestCase):
    def test_restarts_gateway_and_shutdown_stops_the_active_process_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            counter = root / 'starts'
            ready = root / 'ready'
            heartbeat = root / 'heartbeat'
            worker_script = root / 'worker.py'
            worker_script.write_text('''from pathlib import Path
import sys
import time
path = Path(sys.argv[1])
while True:
    path.write_text(str(time.monotonic()))
    time.sleep(0.03)
''')
            fake_hermes = root / 'fake-hermes'
            fake_hermes.write_text('''#!/bin/sh
count=0
[ ! -f "$TEST_START_COUNT" ] || count=$(cat "$TEST_START_COUNT")
count=$((count + 1))
echo "$count" > "$TEST_START_COUNT"
if [ "$count" -eq 1 ]; then exit 23; fi
echo started > "$TEST_READY_PID"
exec "$TEST_PYTHON" "$TEST_WORKER_SCRIPT" "$TEST_HEARTBEAT"
''')
            fake_hermes.chmod(0o755)
            env = {
                **os.environ,
                'HERMES_WORKER_REGISTRY': str(root / 'registry'),
                'HERMES_PYTHON': os.sys.executable,
                'HERMES_AGENT_BUDGET_SCRIPT': str(Path(__file__).parents[1] / 'scripts/agent-budget.py'),
                'HERMES_BIN': str(fake_hermes),
                'HERMES_GATEWAY_RESTART_DELAY_SECONDS': '0.1',
                'TEST_START_COUNT': str(counter),
                'TEST_READY_PID': str(ready),
                'TEST_PYTHON': os.sys.executable,
                'TEST_WORKER_SCRIPT': str(worker_script),
                'TEST_HEARTBEAT': str(heartbeat),
            }
            supervisor = subprocess.Popen(['sh', str(SCRIPT)], env=env,
                                          stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL,
                                          start_new_session=True)
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not ready.exists():
                    if supervisor.poll() is not None:
                        self.fail('supervisor exited before restarting its gateway')
                    time.sleep(0.02)
                self.assertTrue(ready.exists(), 'second gateway did not start')
                self.assertGreaterEqual(int(counter.read_text()), 2)
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline and not heartbeat.exists():
                    time.sleep(0.01)
                self.assertTrue(heartbeat.exists(), 'second gateway worker is not running')
                supervisor.send_signal(signal.SIGTERM)
                supervisor.wait(timeout=5)
                before = heartbeat.stat().st_mtime_ns
                time.sleep(0.15)
                if heartbeat.stat().st_mtime_ns != before:
                    self.fail('active gateway worker survived supervisor shutdown')
            finally:
                if supervisor.poll() is None:
                    supervisor.kill()
                    supervisor.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
