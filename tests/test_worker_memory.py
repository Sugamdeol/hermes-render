import os
from pathlib import Path
import subprocess
import sys
import unittest
import tempfile
import runpy
from unittest.mock import patch


class WorkerMemoryTests(unittest.TestCase):
    def test_threads_start_under_heap_pressure(self):
        script = Path(__file__).parents[1] / 'scripts/worker-memory.py'
        code = '''import runpy, threading, sys, os
runpy.run_path(sys.argv[1])['apply_worker_limit']()
assert threading.stack_size() == 2*1048576
# stack_size() without an argument resets the default, so set it again.
threading.stack_size(2*1048576)
payload = bytearray(128*1048576)
release = threading.Event()
threads = []
try:
    for _ in range(12):
        thread = threading.Thread(target=release.wait)
        thread.start()
        threads.append(thread)
    assert os.environ['HERMES_CRON_MAX_PARALLEL'] == '1'
    print('twelve threads started under 192 MiB data cap', flush=True)
finally:
    release.set()
    for thread in threads: thread.join()
'''
        result = subprocess.run([sys.executable, '-c', code, str(script)], env={**os.environ, 'MALLOC_ARENA_MAX': '1', 'HERMES_PYTHON_DATA_MB': '192', 'HERMES_PYTHON_AS_MB': '0'}, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('twelve threads started', result.stdout)

    def test_cache_heavy_render_snapshot_is_not_agent_pressure(self):
        helper = runpy.run_path(Path(__file__).parents[1] / 'scripts/worker-memory.py')['container_memory']
        with tempfile.TemporaryDirectory() as tmp:
            current = Path(tmp) / 'memory.current'
            stat = Path(tmp) / 'memory.stat'
            current.write_text(str(int(505.4 * 1048576)))
            stat.write_text('file ' + str(384 * 1048576) + '\ninactive_file ' + str(int(66.7 * 1048576)) + '\n')
            with patch.dict(os.environ, {'HERMES_TOTAL_MEMORY_FILE': str(current)}):
                total, noncache = helper()
            self.assertGreater(total, 500 * 1048576)
            self.assertLess(noncache, 122 * 1048576)
            stat.write_text('file 402653184\nshmem 104857600\nfile_dirty 10485760\nfile_writeback 5242880\nunevictable 1048576\n')
            with patch.dict(os.environ, {'HERMES_TOTAL_MEMORY_FILE': str(current)}):
                _, noncache = helper()
            self.assertGreater(noncache, 237 * 1048576)

    def test_lightweight_profile_keeps_existing_allocation_limits(self):
        script = Path(__file__).parents[1] / 'scripts/worker-memory.py'
        code = '''import runpy, resource, subprocess, sys
before = resource.getrlimit(resource.RLIMIT_DATA), resource.getrlimit(resource.RLIMIT_AS)
runpy.run_path(sys.argv[1])['apply_worker_limit']()
after = resource.getrlimit(resource.RLIMIT_DATA), resource.getrlimit(resource.RLIMIT_AS)
assert before == after
payload = bytearray(128*1048576)
print('allocation allowed; limits unchanged', flush=True)
'''
        result = subprocess.run([sys.executable, '-c', code, str(script)], env={**os.environ, 'HERMES_PYTHON_DATA_MB': '96', 'HERMES_PYTHON_AS_MB': '0'}, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('allocation allowed; limits unchanged', result.stdout)
