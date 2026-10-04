import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import signal
import unittest

SCRIPT = Path(__file__).parents[1] / 'scripts/agent-budget.py'
spec = importlib.util.spec_from_file_location('agent_budget', SCRIPT)
budget = importlib.util.module_from_spec(spec)
spec.loader.exec_module(budget)


class AgentBudgetTests(unittest.TestCase):
    def test_gateway_survives_cache_heavy_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            current = Path(tmp) / 'memory.current'
            current.write_text(str(510 * 1048576))
            (Path(tmp) / 'memory.stat').write_text('file ' + str(384 * 1048576) + '\ninactive_file 0\n')
            env = {**os.environ, 'HERMES_WORKER_REGISTRY': tmp + '/workers', 'HERMES_AGENT_CGROUP': '/proc/hermes-no-delegation', 'HERMES_TOTAL_MEMORY_FILE': str(current)}
            monitor = subprocess.Popen([sys.executable, str(SCRIPT), 'monitor'], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            worker = subprocess.Popen([sys.executable, str(SCRIPT), 'run', 'gateway', sys.executable, '-c', 'import time; time.sleep(30)'], env=env, start_new_session=True)
            try:
                time.sleep(2)
                self.assertIsNone(worker.poll(), 'cache alone killed the gateway')
                self.assertIsNone(monitor.poll())
                # Real non-cache pressure must still shed the worker.
                (Path(tmp) / 'memory.stat').write_text('file ' + str(60 * 1048576) + '\n')
                time.sleep(1)
                self.assertIsNone(worker.poll(), 'observe-only mode cancelled a worker')
            finally:
                worker.kill()
                worker.wait()
                monitor.kill()
                monitor.wait()

    def test_descendants_and_detached_tracked_children(self):
        table = {10: (1, 10, '100', 10), 11: (10, 10, '101', 20), 12: (1, 12, '102', 30), 20: (1, 20, '200', 40)}
        selected = budget.descendants(table, {10: 'chat'}, {12: ('102', 'chat'), 20: ('old-pid', 'gateway')})
        self.assertEqual(selected, {10: 'chat', 11: 'chat', 12: 'chat'})

    def test_observer_keeps_busy_worker_tree_alive(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, 'HERMES_WORKER_REGISTRY': tmp + '/workers', 'HERMES_AGENT_RAM_MB': '128', 'HERMES_AGENT_CGROUP': '/proc/hermes-no-delegation', 'HERMES_TOTAL_MEMORY_FILE': tmp + '/total'}
            Path(tmp + '/total').write_text('0')
            monitor = subprocess.Popen([sys.executable, str(SCRIPT), 'monitor'], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
            worker = None
            try:
                deadline = time.monotonic() + 5
                while not Path(env['HERMES_WORKER_REGISTRY']).exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                child = 'import time; chunks=[];\nfor i in range(40): chunks.append(bytearray(2*1048576)); time.sleep(.03)\ntime.sleep(60)'
                parent = 'import subprocess,sys,time; p=subprocess.Popen([sys.executable,"-c",sys.argv[1]]); print(p.pid,flush=True); chunks=[];\nfor i in range(40): chunks.append(bytearray(2*1048576)); time.sleep(.03)\ntime.sleep(60)'
                worker = subprocess.Popen([sys.executable, str(SCRIPT), 'run', 'chat', sys.executable, '-c', parent, child], env=env, stdout=subprocess.PIPE, text=True, start_new_session=True)
                backend = int(worker.stdout.readline().strip())
                time.sleep(3)
                self.assertIsNone(worker.poll())
                self.assertIn(backend, budget.processes())
                self.assertIsNone(monitor.poll())
                self.assertIsNone(unrelated.poll())
            finally:
                if worker is not None:
                    try:
                        os.killpg(worker.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                for proc in (worker, unrelated, monitor):
                    if proc is not None:
                        proc.kill()
                        proc.wait()
                        if proc.stdout:
                            proc.stdout.close()
