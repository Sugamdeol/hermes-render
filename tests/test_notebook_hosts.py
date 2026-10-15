import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
def load():
    spec=importlib.util.spec_from_file_location('dual_notebook',ROOT/'run-colab.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

class NotebookHosts(unittest.TestCase):
    def test_kaggle_detection_and_private_paths(self):
        with patch.dict(os.environ,{'KAGGLE_KERNEL_RUN_TYPE':'Interactive'},clear=True):
            m=load()
        self.assertEqual(m.NOTEBOOK_HOST,'kaggle')
        self.assertEqual(m.NOTEBOOK_ROOT,Path('/kaggle/temp/hermes'))
        self.assertFalse(str(m.LOG).startswith('/kaggle/working'))
        self.assertEqual(m.DATA,Path('/opt/data'))
        self.assertFalse(str(m.SOURCE).startswith('/content'))

    def test_environment_secrets_take_precedence(self):
        m=load()
        with patch.object(m,'NOTEBOOK_HOST','kaggle'),patch.dict(os.environ,{'GIT_STATE_TOKEN':'private'}),patch.dict(sys.modules,{'kaggle_secrets':None}):
            self.assertEqual(m.get_bootstrap_secret('GIT_STATE_TOKEN','token:'),'private')

    def test_kaggle_secrets_used_without_colab_or_prompt(self):
        m=load();client=Mock();client.get_secret.return_value='private'
        fake=types.ModuleType('kaggle_secrets');fake.UserSecretsClient=Mock(return_value=client)
        with patch.object(m,'NOTEBOOK_HOST','kaggle'),patch.dict(os.environ,{},clear=True),patch.dict(sys.modules,{'kaggle_secrets':fake,'google.colab':None}),patch.object(m.getpass,'getpass',side_effect=AssertionError('unexpected input')):
            self.assertEqual(m.get_bootstrap_secret('GIT_STATE_TOKEN','token:'),'private')
            self.assertEqual(m.optional_colab_setting('CLOUDFLARE_TUNNEL_TOKEN'),'private')
        client.get_secret.assert_any_call('GIT_STATE_TOKEN')

    def test_missing_kaggle_secret_fails_without_exposing_provider_error(self):
        m=load();fake=types.ModuleType('kaggle_secrets');fake.UserSecretsClient=Mock(side_effect=RuntimeError('sensitive provider detail'))
        with patch.object(m,'NOTEBOOK_HOST','kaggle'),patch.dict(os.environ,{},clear=True),patch.dict(sys.modules,{'kaggle_secrets':fake}):
            with self.assertRaisesRegex(RuntimeError,'GIT_STATE_TOKEN') as caught:m.get_bootstrap_secret('GIT_STATE_TOKEN','token:')
            self.assertNotIn('sensitive',str(caught.exception))
            self.assertEqual(m.optional_colab_setting('OPTIONAL_MISSING'),'')

    def test_colab_secret_and_prompt_fallback_are_preserved(self):
        m=load();fake=types.ModuleType('google.colab');fake.userdata=Mock();fake.userdata.get.return_value='colab-secret'
        with patch.object(m,'NOTEBOOK_HOST','colab'),patch.dict(os.environ,{},clear=True),patch.dict(sys.modules,{'google.colab':fake}):
            self.assertEqual(m.get_bootstrap_secret('GIT_STATE_TOKEN','token:'),'colab-secret')
            fake.userdata.get.side_effect=RuntimeError('not enabled')
            with patch.object(m.getpass,'getpass',return_value='prompted'):
                self.assertEqual(m.get_bootstrap_secret('GIT_STATE_TOKEN','token:'),'prompted')

    def test_kaggle_cache_never_mounts_drive(self):
        m=load();fake=types.ModuleType('google.colab');fake.drive=Mock()
        with tempfile.TemporaryDirectory() as td,patch.object(m,'NOTEBOOK_HOST','kaggle'),patch.object(m,'NOTEBOOK_ROOT',Path(td)),patch.dict(os.environ,{'HERMES_COLAB_USE_DRIVE_CACHE':'1'},clear=True),patch.dict(sys.modules,{'google.colab':fake}):
            self.assertEqual(m.prepare_cache(),Path(td)/'hermes-runtime-cache')
            fake.drive.mount.assert_not_called()

    def test_runtime_env_uses_host_paths_but_retains_full_profile(self):
        m=load()
        with patch.object(m,'NOTEBOOK_HOST','kaggle'),patch.object(m,'SOURCE',Path('/kaggle/temp/hermes/hermes-render-source')),patch.object(m,'cache_directory',return_value=Path('/tmp/public-cache')):
            env=m.runtime_env('token','key','password')
        self.assertEqual(env['HERMES_NOTEBOOK_HOST'],'kaggle')
        self.assertEqual(env['GIT_STATE_WORKDIR'],'/kaggle/temp/hermes/hermes-git-cache/state')
        self.assertEqual(env['HERMES_RUNTIME_PROFILE'],'colab-full')
        self.assertEqual(env['GIT_STATE_REPO'],'Sugamdeol/hermes-storage')

    def test_kaggle_dashboard_skips_colab_embedding(self):
        m=load();agent=m.ColabAgent(Mock(),{})
        fake=types.ModuleType('google.colab');fake.output=Mock()
        with patch.object(m,'NOTEBOOK_HOST','kaggle'),patch.dict(sys.modules,{'google.colab':fake}),patch.object(m.ColabAgent,'start_tunnel',return_value=None),patch.object(agent,'password'):
            result=agent.dashboard()
        self.assertIsNone(result['colab']);fake.output.eval_js.assert_not_called()

    def test_notebook_has_no_outputs_and_chooses_host_before_writing_launcher(self):
        notebook=json.loads((ROOT/'Hermes.ipynb').read_text())
        for cell in notebook['cells']:
            if cell['cell_type']=='code':self.assertFalse(cell['outputs']);self.assertIsNone(cell['execution_count'])
        source=''.join(notebook['cells'][1]['source'])
        compile(source,'Hermes.ipynb','exec')
        self.assertIn('/kaggle/temp/hermes',source)
        self.assertLess(source.index('launcher_root.mkdir'),source.index('launcher_path.write_bytes'))
        self.assertIn('HERMES = HERMES_COLAB',source)

    def test_required_encryption_key_is_not_trimmed(self):
        m=load()
        with patch.dict(os.environ,{'STORAGE_ENCRYPTION_KEY':' exact key '}):
            self.assertEqual(m.get_bootstrap_secret('STORAGE_ENCRYPTION_KEY','key:'),' exact key ')
