import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

isolation = load('notebook_isolation', 'scripts/notebook-host-isolation.py')
launcher = load('isolated_launcher', 'run-colab.py')


class NotebookIsolationTests(unittest.TestCase):
    def test_restored_projects_and_profiles_use_local_files(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            workspace = home / 'shared-storage/workspace'
            workspace.mkdir(parents=True)
            (workspace / 'project.txt').write_text('saved work')
            config = home / 'config.yaml'
            config.write_text('terminal:\n  cwd: /storage/workspace\nmodel:\n  default: private-model\n')
            profile = home / 'profiles/test/config.yaml'
            profile.parent.mkdir(parents=True)
            profile.write_text('terminal:\n  cwd: /data/hermes/state/profiles/test/work\n')
            with patch.dict(os.environ, {'HERMES_NOTEBOOK_HOST': 'colab'}):
                isolation.apply(home)
                first = config.read_text()
                isolation.apply(home)
            self.assertEqual(config.read_text(), first)
            self.assertEqual(yaml.safe_load(first)['terminal']['cwd'], str(workspace))
            self.assertEqual(yaml.safe_load(first)['model']['default'], 'private-model')
            self.assertEqual((workspace / 'project.txt').read_text(), 'saved work')
            self.assertEqual(yaml.safe_load(profile.read_text())['terminal']['cwd'], str(home / 'profiles/test/work'))

    def test_scrubs_host_wiring_but_preserves_credentials(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            env = home / '.env'
            env.write_text('OPENAI_API_KEY=keep-me\nTERMINAL_CWD=/storage/workspace\nINSTABOX_MACHINE=old\n')
            runtime = home / '.render-runtime-env.json'
            runtime.write_text(json.dumps({'version': 1, 'variables': {'PROVIDER_TOKEN': 'keep', 'GIT_STATE_WORKDIR': '/data/hermes/git', 'INSTABOX_API_KEY': 'old'}}))
            with patch.dict(os.environ, {'HERMES_NOTEBOOK_HOST': 'kaggle'}):
                isolation.apply(home)
            self.assertEqual(env.read_text(), 'OPENAI_API_KEY=keep-me\n')
            self.assertEqual(json.loads(runtime.read_text())['variables'], {'PROVIDER_TOKEN': 'keep'})
            self.assertEqual(env.stat().st_mode & 0o777, 0o600)

    def test_other_hosts_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            config = home / 'config.yaml'
            config.write_text('terminal: {cwd: /storage/workspace}\n')
            original = config.read_bytes()
            for host in ('instabox', ''):
                with patch.dict(os.environ, {'HERMES_NOTEBOOK_HOST': host}):
                    isolation.apply(home)
                self.assertEqual(config.read_bytes(), original)
                self.assertFalse((home / 'workspace').exists())

    def test_custom_paths_preserved_and_traversal_rejected(self):
        self.assertEqual(isolation.local_path('/content/myproject', '/opt/data'), '/content/myproject')
        with self.assertRaises(ValueError):
            isolation.local_path('/storage/../../etc', '/opt/data')

    def test_notebook_environment_overrides_saved_host_defaults(self):
        for host in ('colab', 'kaggle'):
            with tempfile.TemporaryDirectory() as td, patch.object(launcher, 'NOTEBOOK_HOST', host), patch.object(launcher, 'cache_directory', return_value=Path(td)):
                env = launcher.runtime_env('token', 'key', 'password')
            for key in ('GIT_STATE_BASE_FILE', 'HERMES_DASHBOARD_FILES_ROOT', 'CLOUDFLARE_TUNNEL_TOKEN', 'PYTHONPATH', 'PYTHONHOME'):
                self.assertIn(key, env['HERMES_ENV_OVERRIDE_KEYS'])
            self.assertEqual(env['CLOUDFLARE_TUNNEL_TOKEN'], '')
            self.assertNotIn('/data/hermes', env['GIT_STATE_BASE_FILE'])

    def test_cleanup_precedes_loading_saved_environment(self):
        bootstrap = (ROOT / 'scripts/bootstrap.sh').read_text()
        self.assertLess(bootstrap.index('notebook-host-isolation.py'), bootstrap.index('--load-existing'))


if __name__ == '__main__':
    unittest.main()
