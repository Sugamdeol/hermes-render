import os
from pathlib import Path
import subprocess
import sys
import unittest
import tempfile
import runpy
from unittest.mock import patch


class WorkerMemoryTests(unittest.TestCase):
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

    def test_kernel_rejects_allocation_and_preserves_limits_in_child(self):
        script = Path(__file__).parents[1] / 'scripts/worker-memory.py'
        code = '''import runpy, resource, subprocess, sys
limit = runpy.run_path(sys.argv[1])
limit['apply_worker_limit']()
assert resource.getrlimit(resource.RLIMIT_DATA) == (96*1048576, 96*1048576)
try:
    payload = bytearray(128*1048576)
except MemoryError:
    print('allocation refused by kernel', flush=True)
else:
    raise AssertionError('allocation bypassed kernel cap')
subprocess.run([sys.executable, '-c', 'import resource; assert resource.getrlimit(resource.RLIMIT_DATA)==(96*1048576,96*1048576)'], check=True)
'''
        result = subprocess.run([sys.executable, '-c', code, str(script)], env={**os.environ, 'HERMES_PYTHON_DATA_MB': '96', 'HERMES_PYTHON_AS_MB': '0'}, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('allocation refused by kernel', result.stdout)
