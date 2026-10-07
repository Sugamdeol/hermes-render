import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch, Mock


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

    def test_colab_secret_reuses_saved_account_value(self):
        from types import ModuleType, SimpleNamespace
        module = load('run-colab')
        fake = ModuleType('google.colab')
        fake.userdata = SimpleNamespace(get=lambda name:'saved-'+name)
        with patch.dict(os.environ, {}, clear=True), patch.dict(sys.modules, {'google.colab':fake}), patch.object(module.getpass, 'getpass') as prompt:
            self.assertEqual(module.get_bootstrap_secret('GIT_STATE_TOKEN','token: '), 'saved-GIT_STATE_TOKEN')
        prompt.assert_not_called()

    def test_colab_secret_failure_uses_hidden_prompt(self):
        from types import ModuleType, SimpleNamespace
        module = load('run-colab')
        fake = ModuleType('google.colab')
        def missing(name): raise RuntimeError('secret unavailable')
        fake.userdata = SimpleNamespace(get=missing)
        with patch.dict(os.environ, {}, clear=True), patch.dict(sys.modules, {'google.colab':fake}), patch.object(module.getpass, 'getpass', return_value='hidden-value') as prompt:
            self.assertEqual(module.get_bootstrap_secret('STORAGE_ENCRYPTION_KEY','key: '),'hidden-value')
        prompt.assert_called_once_with('key: ')

    def test_colab_repeat_start_keeps_same_running_agent(self):
        from types import SimpleNamespace
        module = load('run-colab')
        agent = SimpleNamespace(process=SimpleNamespace(poll=lambda:None), password=Mock())
        module.HERMES_COLAB = agent
        with patch('builtins.input', side_effect=AssertionError('must not prompt')), patch.object(module, 'install', side_effect=AssertionError('must not reinstall')):
            self.assertIs(module.main(confirm_switch=True), agent)

    def test_colab_notebook_has_one_executable_cell_and_verified_launcher(self):
        import hashlib, json
        notebook = json.loads((ROOT/'Hermes.ipynb').read_text())
        cells = [cell for cell in notebook['cells'] if cell['cell_type']=='code']
        self.assertEqual(len(cells),1)
        source = ''.join(cells[0]['source'])
        compile(source,'Hermes.ipynb','exec')
        self.assertIn(hashlib.sha256((ROOT/'run-colab.py').read_bytes()).hexdigest(),source)
        self.assertIn('confirm_switch=True',source)
        self.assertIn('finally:',source)
        self.assertEqual(cells[0]['outputs'],[])

    def test_colab_forces_dashboard_enabled_on_private_port(self):
        module = load('run-colab')
        with patch.dict(os.environ, {'HERMES_DASHBOARD':'0'}, clear=True):
            env = module.runtime_env('token','key')
        self.assertEqual(env['HERMES_DASHBOARD'],'1')
        self.assertEqual(env['HERMES_DASHBOARD_HOST'],'127.0.0.1')
        self.assertEqual(env['HERMES_DASHBOARD_PORT'],'9119')
        for key in ('HERMES_DASHBOARD','HERMES_DASHBOARD_HOST','HERMES_DASHBOARD_PORT'):
            self.assertIn(key,env['HERMES_ENV_OVERRIDE_KEYS'].split(','))

    def test_colab_dashboard_button_uses_proxy_and_keeps_credentials_private(self):
        from types import ModuleType, SimpleNamespace
        module = load('run-colab')
        fake = ModuleType('google.colab')
        url = 'https://runtime.example.test/?value="quoted"'
        fake.output = SimpleNamespace(eval_js=lambda code:url)
        display_module = ModuleType('IPython.display')
        display_module.HTML = lambda html:html
        shown = []
        display_module.display = shown.append
        with patch.dict(sys.modules, {'google.colab':fake, 'IPython.display':display_module}):
            agent = module.ColabAgent(None, {'GIT_STATE_TOKEN':'private-token'})
            with patch.object(agent, 'password') as password:
                self.assertEqual(agent.dashboard(),url)
                password.assert_called_once_with()
        self.assertIn('Open Dashboard',shown[0])
        self.assertIn('&quot;quoted&quot;',shown[0])
        self.assertNotIn('private-token',shown[0])


if __name__ == '__main__':
    unittest.main()
