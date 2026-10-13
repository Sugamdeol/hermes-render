import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('fixed_launcher', ROOT / 'run-colab.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class FixedDashboardTests(unittest.TestCase):
    def settings(self, token='', url=''):
        return patch.object(launcher, 'optional_colab_setting', side_effect=lambda name: {
            'CLOUDFLARE_TUNNEL_TOKEN': token, 'HERMES_DASHBOARD_URL': url}[name])

    def test_named_settings_validate_and_normalize(self):
        with self.settings('test-secret', 'https://hermes.example.com/'):
            self.assertEqual(launcher.dashboard_tunnel_settings(), ('test-secret', 'https://hermes.example.com'))
        with self.settings():
            self.assertEqual(launcher.dashboard_tunnel_settings(), ('', ''))

    def test_partial_or_unsafe_configuration_does_not_pick_random_url(self):
        with self.settings(url='https://hermes.example.com'):
            with self.assertRaises(RuntimeError):
                launcher.dashboard_tunnel_settings()
        for url in ('http://hermes.example.com', 'https://old.trycloudflare.com',
                    'https://user:password@example.com', 'https://example.com/path'):
            with self.settings('test-secret', url):
                with self.assertRaises(ValueError):
                    launcher.dashboard_tunnel_settings()

    def test_named_tunnel_uses_private_token_file_and_same_url_after_restarts(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            real_path = Path
            def redirected(value):
                return home if str(value) == '/content/hermes-cloudflare' else real_path(value)
            response = MagicMock()
            response.__enter__.return_value.status = 200
            processes = []
            def spawn(command, **kwargs):
                self.assertNotIn('test-secret', command)
                self.assertNotIn('CLOUDFLARE_TUNNEL_TOKEN', kwargs['env'])
                token_path = Path(command[command.index('--token-file') + 1])
                self.assertEqual(token_path.read_text(), 'test-secret')
                self.assertEqual(token_path.stat().st_mode & 0o777, 0o600)
                proc = MagicMock()
                proc.poll.return_value = None
                processes.append(proc)
                return proc
            agent = launcher.ColabAgent.__new__(launcher.ColabAgent)
            with self.settings('test-secret', 'https://hermes.example.com'), patch.object(launcher, 'Path', side_effect=redirected), patch.object(launcher, 'install_cloudflared', return_value='/usr/bin/cloudflared'), patch.object(launcher.subprocess, 'Popen', side_effect=spawn), patch.object(launcher.urllib.request, 'urlopen', return_value=response):
                for _ in range(2):
                    self.assertEqual(agent.start_tunnel(), 'https://hermes.example.com')
                    self.assertEqual(agent.start_tunnel(), 'https://hermes.example.com')
                    token_path = Path(agent.tunnel_token_file)
                    agent.stop_tunnel()
                    self.assertFalse(token_path.exists())
                self.assertEqual(len(processes), 2)

    def test_quick_tunnel_still_works_without_configuration(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            real_path = Path
            def redirected(value):
                return home if str(value) == '/content/hermes-cloudflare' else real_path(value)
            def spawn(command, **kwargs):
                self.assertIn('--url', command)
                kwargs['stdout'].write('https://temporary.trycloudflare.com\n')
                kwargs['stdout'].flush()
                proc = MagicMock()
                proc.poll.return_value = None
                return proc
            agent = launcher.ColabAgent.__new__(launcher.ColabAgent)
            with self.settings(), patch.object(launcher, 'Path', side_effect=redirected), patch.object(launcher, 'install_cloudflared', return_value='/usr/bin/cloudflared'), patch.object(launcher.subprocess, 'Popen', side_effect=spawn):
                self.assertEqual(agent.start_tunnel(), 'https://temporary.trycloudflare.com')
                agent.stop_tunnel()

    def test_tunnel_credentials_do_not_enter_agent_environment(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {'CLOUDFLARE_TUNNEL_TOKEN': 'test-secret'}), patch.object(launcher, 'cache_directory', return_value=Path(td)):
            env = launcher.runtime_env('git-token', 'encryption-key')
            self.assertNotIn('CLOUDFLARE_TUNNEL_TOKEN', env)
