import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('progress_launcher', ROOT / 'run-colab.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class StartupProgressTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name)
        self.now = 0

    def make(self, warm=False):
        return launcher.StartupProgress(self.cache, warm, clock=lambda:self.now)

    def test_first_start_has_no_invented_eta_and_elapsed_updates(self):
        progress = self.make()
        self.now = 74
        view = progress.snapshot()
        self.assertEqual(view['elapsed'], '1m 14s')
        self.assertIn('Calculating', view['eta'])
        self.assertEqual(view['percent'], 0)

    def test_completed_steps_are_monotonic_and_log_secrets_stay_out_of_ui(self):
        progress = self.make()
        with patch.object(progress, 'render'):
            progress.observe('[render-tools] state restore finished\nTOKEN=secret-value')
            progress.observe('[render-tools] starting one foreground, restartable Telegram gateway')
            progress.step(1)
        view = progress.snapshot()
        self.assertEqual(view['percent'], 60)
        self.assertEqual(view['stage'], 'Starting dashboard and Telegram')
        self.assertNotIn('secret-value', str(view))

    def test_success_records_durations_and_estimate_counts_down(self):
        first = self.make()
        with patch.object(first, 'render'):
            for index in range(1,5):
                self.now = index * 10
                first.step(index)
            self.now = 50
            first.finish()
        self.assertEqual(first.snapshot()['percent'], 100)
        self.now = 100
        next_start = self.make()
        self.now = 104
        self.assertIn('0m 46s left', next_start.snapshot()['eta'])
        self.now = 112
        self.assertIn('Taking longer', next_start.snapshot()['eta'])
        self.assertNotIn('left', next_start.snapshot()['eta'])
        self.assertIn('Calculating', self.make(warm=True).snapshot()['eta'])

    def test_bad_history_cannot_inject_labels_or_nonfinite_estimates(self):
        for content in ('{broken', '[]', json.dumps({'fresh':{stage:float('nan') for stage in launcher.StartupProgress.STAGES}})):
            (self.cache / 'startup-timings.json').write_text(content)
            self.assertIn('Calculating', self.make().snapshot()['eta'])

    def test_failure_stops_refresh_without_recording_success(self):
        progress = self.make()
        with patch.object(progress, 'render'):
            with self.assertRaises(RuntimeError):
                with progress:
                    raise RuntimeError('startup failed')
        self.assertFalse(progress.thread.is_alive())
        self.assertIn('attention', progress.snapshot()['stage'])
        self.assertFalse(progress.path.exists())

    def test_display_failure_cannot_interrupt_startup(self):
        progress = self.make()
        with patch.object(progress, '_render', side_effect=RuntimeError('notebook closed')):
            progress.step(1)
            progress.finish()
        self.assertEqual(progress.state, 'ready')

    def test_launch_records_ready_only_after_gateway_health_and_dashboard(self):
        progress = Mock()
        process = Mock()
        process.poll.return_value = None
        agent = Mock()
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"gateway_running":true,"gateway_state":"running"}'
        with patch.object(launcher, 'start_update_control') as update_control, patch.object(launcher, 'LOG', self.cache / 'startup.log'), patch.object(launcher, 'install'), patch.object(launcher.subprocess, 'Popen', return_value=process), patch.object(launcher, 'ColabAgent', return_value=agent), patch.object(launcher.urllib.request, 'urlopen', return_value=response), patch.dict(launcher.__dict__, {'HERMES_COLAB':None}):
            self.assertIs(launcher.launch_with_progress({}, progress), agent)
            self.assertIs(launcher.HERMES_COLAB, agent)
        update_control.assert_called_once()
        progress.finish.assert_called_once()
        agent.dashboard.assert_called_once()
        self.assertEqual(progress.step.call_args_list[-1].args, (4,))


if __name__ == '__main__':
    unittest.main()
