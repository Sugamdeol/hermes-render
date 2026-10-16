import importlib.util
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('boot_guard_patch', ROOT/'scripts/patch-native-task-recovery.py')
helper = importlib.util.module_from_spec(spec); spec.loader.exec_module(helper)
SOURCE = '''class Runner:
    def __init__(self): self.pending = []
    def candidates(self):
        candidates = self.pending
        if candidates:
            try:
                from gateway import restart_loop_guard as _rlg
                _max_restarts, _window, _max_gap = 3, 300, 300
                if _rlg.check_and_record(_max_restarts, _window, max_gap_seconds=_max_gap):
                    return None
            except Exception:
                pass
        return candidates
'''


class BootCounter(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name); (self.root/'gateway').mkdir()
        self.path = self.root/'gateway/run_startup.py'; self.path.write_text(SOURCE)
        helper.patch_boot_guard(self.root)
        ns = {}; exec(compile(self.path.read_text(), str(self.path), 'exec'), ns)
        self.Runner = ns['Runner']
        self.guard = Mock(return_value=False)
        gateway = ModuleType('gateway'); gateway.restart_loop_guard = ModuleType('restart_loop_guard')
        gateway.restart_loop_guard.check_and_record = self.guard
        modules = patch.dict(sys.modules, {'gateway': gateway}); modules.start(); self.addCleanup(modules.stop)

    def test_repeated_scans_count_one_boot_and_new_runner_counts_again(self):
        runner = self.Runner(); runner.pending = ['interrupted']
        for _ in range(20): self.assertEqual(runner.candidates(), ['interrupted'])
        self.assertEqual(self.guard.call_count, 1)
        other = self.Runner(); other.pending = ['interrupted']; other.candidates()
        self.assertEqual(self.guard.call_count, 2)

    def test_tripped_guard_stays_blocked_without_incrementing_on_each_scan(self):
        self.guard.return_value = True
        runner = self.Runner(); runner.pending = ['interrupted']
        for _ in range(20): self.assertIsNone(runner.candidates())
        self.assertEqual(self.guard.call_count, 1)

    def test_empty_scan_does_not_record_boot_but_later_pending_scan_does(self):
        runner = self.Runner(); self.assertEqual(runner.candidates(), [])
        self.guard.assert_not_called()
        runner.pending = ['interrupted']; runner.candidates(); self.guard.assert_called_once()

    def test_idempotent_and_supports_older_module_layout(self):
        once = self.path.read_text(); helper.patch_boot_guard(self.root)
        self.assertEqual(once, self.path.read_text())
        self.path.unlink(); old = self.root/'gateway/run.py'; old.write_text(SOURCE)
        helper.patch_boot_guard(self.root); self.assertEqual(old.read_text(), once)
