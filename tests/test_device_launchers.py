from pathlib import Path
import os
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class AndroidLauncherTests(unittest.TestCase):
    def run_android(self, command, session=False, reply=''):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / 'commands'
            for name, body in {
                'proot-distro': 'echo "proot:$*" >> "$CALL_LOG"; exit 0',
                'tmux': 'echo "tmux:$*" >> "$CALL_LOG"; if [ "$1" = has-session ]; then exit "$SESSION_EXIT"; fi',
                'termux-wake-lock': 'echo wake >> "$CALL_LOG"',
            }.items():
                path = root / name
                path.write_text('#!/bin/bash\n' + body + '\n')
                path.chmod(0o755)
            env = {**os.environ, 'HOME': directory, 'PREFIX': '/data/data/com.termux/files/usr',
                   'PATH': directory + ':' + os.environ['PATH'], 'CALL_LOG': str(log),
                   'SESSION_EXIT': '0' if session else '1'}
            result = subprocess.run(['bash', str(ROOT / 'run-android.sh'), command],
                                    env=env, input=reply, text=True, capture_output=True)
            return result, log.read_text() if log.exists() else ''

    def test_start_cancel_never_launches_gateway(self):
        result, log = self.run_android('start', reply='no\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('new-session', log)

    def test_start_uses_separate_tmux_and_wake_lock(self):
        result, log = self.run_android('start', reply='SWITCH\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('wake', log)
        self.assertIn('new-session -d -s hermes-android', log)

    def test_repeated_start_does_not_spawn_second_gateway(self):
        result, log = self.run_android('start', session=True)
        self.assertEqual(result.returncode, 0)
        self.assertNotIn('new-session', log)

    def test_update_refuses_running_gateway(self):
        result, log = self.run_android('update', session=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('bash hermes update', log)

    def test_dashboard_is_loopback_only(self):
        result, log = self.run_android('dashboard')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('hermes dashboard --host 127.0.0.1 --port 9119 --no-open', log)

    def test_stop_requests_interrupt_without_destroying_storage(self):
        result, log = self.run_android('stop', session=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn('send-keys -t =hermes-android C-c', log)
        self.assertNotIn('kill-session', log)

    def test_help_and_unknown_command(self):
        result, _ = self.run_android('help')
        self.assertEqual(result.returncode, 0)
        result, _ = self.run_android('wrong')
        self.assertEqual(result.returncode, 2)


if __name__ == '__main__':
    unittest.main()
