import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LauncherTests(unittest.TestCase):
    def test_both_hosts_restore_same_repo_and_override_saved_webhooks(self):
        for name in ('run-local', 'run-colab'):
            with self.subTest(name=name), patch.dict(os.environ, {}, clear=True):
                module = load(name)
                env = module.runtime_env('bootstrap-token', 'existing-key')
                self.assertEqual(env['GIT_STATE_REPO'], 'Sugamdeol/hermes-storage')
                self.assertEqual(env['GIT_STATE_BRANCH'], 'state')
                self.assertEqual(env['GIT_STATE_ENV_MODE'], 'encrypt')
                self.assertEqual(env['TELEGRAM_WEBHOOK_URL'], '')
                self.assertEqual(env['RENDER_EXTERNAL_URL'], '')
                overrides = env['HERMES_ENV_OVERRIDE_KEYS'].split(',')
                self.assertIn('TELEGRAM_WEBHOOK_URL', overrides)
                self.assertIn('HERMES_ENV_OVERRIDE_KEYS', overrides)
                self.assertIn('GIT_STATE_WORKDIR', overrides)
                self.assertIn('HERMES_INSTANCE_ID', overrides)
                self.assertIn('HERMES_RECOVERY_CHECKPOINT_FILE', overrides)

    def test_local_help_needs_no_docker_or_credentials(self):
        result = subprocess.run([sys.executable, str(ROOT / 'run-local.py'), '--help'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn('backup', result.stdout)

    def test_colab_does_not_pass_notebook_credentials_into_agent(self):
        module = load('run-colab')
        with patch.dict(os.environ, {'COLAB_RUNTIME_SECRET': 'not-an-agent-key'}, clear=True):
            env = module.runtime_env('token', 'key')
        self.assertNotIn('COLAB_RUNTIME_SECRET', env)

    def test_local_backup_uses_existing_daemon_not_another_git_writer(self):
        module = load('run-local')
        with patch.object(module, 'running', return_value=True), patch.object(module, 'run') as run:
            module.backup()
        self.assertIn('exec', run.call_args.args[0])
        self.assertIn('hermes-recovery-checkpoint.json', run.call_args.args[0][-1])
        self.assertNotIn('git-storage.py', run.call_args.args[0][-1])

    def test_colab_failed_backup_does_not_kill_agent(self):
        module = load('run-colab')
        agent = module.ColabAgent(None, {})
        with patch.object(agent, 'backup', side_effect=RuntimeError('save failed')), \
             patch.object(module.os, 'kill') as kill:
            with self.assertRaisesRegex(RuntimeError, 'save failed'):
                agent.stop()
        kill.assert_not_called()

    def test_local_rejects_env_file_line_injection(self):
        module = load('run-local')
        with self.assertRaises(ValueError):
            module.runtime_env('token\nGIT_STATE_ENV_MODE=plaintext', 'key')


if __name__ == '__main__':
    unittest.main()
