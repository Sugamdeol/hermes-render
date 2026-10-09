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
        with patch('builtins.input', side_effect=AssertionError('must not prompt')), patch.object(module, 'install', side_effect=AssertionError('must not reinstall')), patch.object(module.ColabAgent, 'start_tunnel', return_value=None):
            self.assertIs(module.main(confirm_switch=True), agent)

    def test_colab_agent_account_runs_as_root_like_notebook_cells(self):
        module = load('run-colab')
        self.assertEqual(module.root_account_plan(0), [])
        self.assertEqual(module.root_account_plan(1001), [['usermod', '-o', '-u', '0', 'hermes']])
        self.assertEqual(module.root_account_plan(None),
                         [['groupadd', '-f', 'hermes'],
                          ['useradd', '-m', '-o', '-u', '0', '-g', 'hermes', '-s', '/bin/bash', 'hermes']])

    def test_colab_entrypoint_skips_gosu_drop_when_hermes_is_root(self):
        module = load('run-colab')
        stock = ('#!/bin/bash\nset -e\nif [ "$(id -u)" = "0" ]; then\n'
                 '    echo "Dropping root privileges"\n    exec gosu hermes "$0" "$@"\nfi\n')
        patched = module.keep_root_entrypoint(stock)
        self.assertIn('if [ "$(id -u)" = "0" ] && [ "$(id -u hermes)" != "0" ]; then', patched)
        self.assertEqual(patched.count('gosu hermes'), 1)
        # Idempotent: a cached install may patch the same file again.
        self.assertEqual(module.keep_root_entrypoint(patched), patched)
        with self.assertRaisesRegex(RuntimeError, 'privilege-drop patch'):
            module.keep_root_entrypoint('#!/bin/bash\n# unrelated script\n')

    def test_colab_notebook_has_one_executable_cell_and_verified_launcher(self):
        import hashlib, json
        notebook = json.loads((ROOT/'Hermes.ipynb').read_text())
        cells = [cell for cell in notebook['cells'] if cell['cell_type']=='code']
        self.assertEqual(len(cells),1)
        source = ''.join(cells[0]['source'])
        compile(source,'Hermes.ipynb','exec')
        self.assertRegex(source,r'hexdigest\(\) != "[a-f0-9]{64}"')
        self.assertIn('confirm_switch=True',source)
        self.assertNotIn('/hermes-render/main/run-colab.py',source)
        self.assertRegex(source,r'/hermes-render/[a-f0-9]{40}/run-colab.py')
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

    def test_colab_dashboard_uses_supported_iframe_and_shows_login(self):
        from types import ModuleType, SimpleNamespace
        module = load('run-colab')
        fake = ModuleType('google.colab')
        url = 'https://runtime.example.test/'
        iframe = Mock()
        fake.output = SimpleNamespace(eval_js=lambda code:url, serve_kernel_port_as_iframe=iframe)
        with patch.dict(sys.modules, {'google.colab':fake}):
            agent = module.ColabAgent(None, {'GIT_STATE_TOKEN':'private-token'})
            with patch.object(agent, 'password') as password, patch.object(module.ColabAgent, 'start_tunnel', return_value=None):
                self.assertEqual(agent.dashboard(),{'cloudflare':None,'colab':url})
                password.assert_called_once_with()
        iframe.assert_called_once_with(10000, height=850, cache_in_notebook=False)

    def test_colab_reuses_existing_tunnel(self):
        module = load('run-colab')
        agent = module.ColabAgent(None,{})
        agent.tunnel_process = Mock()
        agent.tunnel_process.poll.return_value = None
        agent.tunnel_url = 'https://active.trycloudflare.com'
        with patch.object(module,'install_cloudflared',side_effect=AssertionError('no second install')):
            self.assertEqual(agent.start_tunnel(),agent.tunnel_url)

    def test_colab_stops_tunnel_and_clears_stale_url(self):
        module = load('run-colab')
        agent = module.ColabAgent(None,{})
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired('cloudflared',5),0]
        agent.tunnel_process = process
        agent.tunnel_url = 'https://old.trycloudflare.com'
        agent.stop_tunnel()
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertIsNone(agent.tunnel_process)
        self.assertIsNone(agent.tunnel_url)

    def test_colab_shows_both_dashboard_urls(self):
        from types import ModuleType, SimpleNamespace
        module = load('run-colab')
        fake = ModuleType('google.colab')
        fake.output = SimpleNamespace(eval_js=lambda code:'https://runtime.colab.dev',serve_kernel_port_as_iframe=Mock())
        display = ModuleType('IPython.display')
        display.HTML = lambda html:html
        display.display = Mock()
        agent = module.ColabAgent(None,{})
        with patch.dict(sys.modules,{'google.colab':fake,'IPython.display':display}), patch.object(module.ColabAgent,'start_tunnel',return_value='https://example.trycloudflare.com'), patch.object(agent,'password') as password:
            urls = agent.dashboard()
        self.assertEqual(urls,{'cloudflare':'https://example.trycloudflare.com','colab':'https://runtime.colab.dev'})
        password.assert_called_once()
        self.assertIn('example.trycloudflare.com',display.display.call_args.args[0])

    def test_cloudflared_direct_download_retries_without_release_api(self):
        module = load('run-colab')
        with patch.object(module.shutil,'which',return_value=None), patch.object(module.urllib.request,'urlopen',side_effect=OSError('download unavailable')) as fetch, patch.object(module.time,'sleep'):
            with self.assertRaisesRegex(OSError,'download unavailable'):
                module.install_cloudflared()
        self.assertEqual(fetch.call_count,3)
        self.assertIn('/releases/download/2026.10.0/',fetch.call_args.args[0].full_url)
        self.assertNotIn('api.github.com',fetch.call_args.args[0].full_url)

    def test_cloudflare_failure_is_visible_without_credentials(self):
        import contextlib,io
        from types import ModuleType,SimpleNamespace
        module = load('run-colab')
        fake = ModuleType('google.colab')
        fake.output = SimpleNamespace(eval_js=lambda code:'https://runtime.colab.dev',serve_kernel_port_as_iframe=Mock())
        agent = module.ColabAgent(None,{'GIT_STATE_TOKEN':'private-token'})
        text = io.StringIO()
        with patch.dict(sys.modules,{'google.colab':fake}), patch.object(module.ColabAgent,'start_tunnel',side_effect=RuntimeError('DNS failed private-token')), patch.object(agent,'password'), contextlib.redirect_stdout(text):
            agent.dashboard()
        self.assertIn('DNS failed',text.getvalue())
        self.assertNotIn('private-token',text.getvalue())


    def test_root_gateway_opt_in_is_colab_only(self):
        colab = load('run-colab')
        with patch.dict(os.environ, {}, clear=True):
            env = colab.runtime_env('token', 'key')
        self.assertEqual(env['HERMES_ALLOW_ROOT_GATEWAY'], '1')
        self.assertIn('HERMES_ALLOW_ROOT_GATEWAY', env['HERMES_ENV_OVERRIDE_KEYS'].split(','))
        local = load('run-local')
        with patch.dict(os.environ, {}, clear=True):
            local_env = local.runtime_env('token', 'key')
        self.assertNotIn('HERMES_ALLOW_ROOT_GATEWAY', local_env)
        self.assertNotIn('HERMES_ALLOW_ROOT_GATEWAY', local_env['HERMES_ENV_OVERRIDE_KEYS'].split(','))

    def test_root_gateway_flag_is_never_set_outside_the_colab_launcher(self):
        # Render, Docker and the local launcher keep upstream's root-gateway guard.
        locations = ['Dockerfile', 'run-local.py', 'run-local.sh', 'render.yaml', '.env.example',
                     'env', 'scripts', 'skills', 'dashboard-plugins', '.github']
        for location in locations:
            path = ROOT / location
            files = [path] if path.is_file() else [p for p in path.rglob('*') if p.is_file()]
            for file in files:
                if '__pycache__' in file.parts or file.suffix == '.pyc':
                    continue
                with self.subTest(file=str(file.relative_to(ROOT))):
                    self.assertNotIn('HERMES_ALLOW_ROOT_GATEWAY', file.read_text(errors='ignore'))

    def test_colab_restart_sets_root_opt_in_before_popen(self):
        source = (ROOT / 'update-chat-ui.py').read_text()
        restart = source.index('HERMES_COLAB.process = subprocess.Popen(')
        self.assertLess(source.index("HERMES_COLAB.env['HERMES_ALLOW_ROOT_GATEWAY'] = '1'"), restart)
        override = next(line for line in source.splitlines()
                        if "HERMES_COLAB.env['HERMES_ENV_OVERRIDE_KEYS'] =" in line)
        self.assertIn("'HERMES_ALLOW_ROOT_GATEWAY'", override)
        self.assertLess(source.index(override), restart)
        self.assertIn('env=HERMES_COLAB.env', source[restart:restart + 200])


if __name__ == '__main__':
    unittest.main()
