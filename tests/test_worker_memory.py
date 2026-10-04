import os
from pathlib import Path
import subprocess
import sys
import unittest


class WorkerMemoryTests(unittest.TestCase):
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
