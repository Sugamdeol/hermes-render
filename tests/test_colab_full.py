import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,ROOT/path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
profile=load('full_profile','scripts/colab-profile.py')
launcher=load('full_launcher','run-colab.py')

class FullColabTests(unittest.TestCase):
    def test_render_limits_reset_but_provider_and_user_edits_survive(self):
        config={'model':{'default':'private-model'},'providers':{'my-api':{'models':{'manual-id':{}}}},'agent':{'max_turns':30,'gateway_timeout':2400},'delegation':{'max_concurrent_children':1,'orchestrator_enabled':False},'compression':{'threshold':0.4}}
        profile.migrate(config)
        self.assertNotIn('max_turns',config['agent'])
        self.assertEqual(config['agent']['gateway_timeout'],2400)
        self.assertEqual(config['model']['default'],'private-model')
        self.assertIn('manual-id',config['providers']['my-api']['models'])
        self.assertNotIn('orchestrator_enabled',config['delegation'])
    def test_migration_and_password_persist_without_reapplying_limits(self):
        with tempfile.TemporaryDirectory() as td:
            home=Path(td);(home/'config.yaml').write_text('agent:\n  max_turns: 30\n')
            (home/'.render-runtime-env.json').write_text(json.dumps({'version':1,'variables':{'HERMES_GATEWAY_TOKEN':'old'}}))
            with patch.dict(os.environ,{'HERMES_GATEWAY_TOKEN':'test-password'}), patch.dict('sys.modules', {'hermes_cli.update_channel':type('Channel',(),{'install_id':staticmethod(lambda p:'install-id'),'_write_channel_record':staticmethod(lambda *a,**k: True)})()}):
                profile.apply(home,home/'install')
                self.assertIn('test-password',(home/'.env').read_text())
                self.assertEqual(json.loads((home/'.render-runtime-env.json').read_text())['variables']['HERMES_GATEWAY_TOKEN'],'test-password')
                (home/'config.yaml').write_text('agent:\n  max_turns: 30\n')
                profile.apply(home,home/'install')
                self.assertIn('max_turns: 30',(home/'config.yaml').read_text())
    def test_launcher_overrides_saved_render_defaults_and_password(self):
        with tempfile.TemporaryDirectory() as td, patch.object(launcher,'cache_directory',return_value=Path(td)):
            env=launcher.runtime_env('test-token','test-key','test-password')
        self.assertEqual(env['HERMES_RUNTIME_PROFILE'],'colab-full')
        self.assertEqual(env['HERMES_GATEWAY_TOKEN'],'test-password')
        self.assertIn('HERMES_GATEWAY_TOKEN',env['HERMES_ENV_OVERRIDE_KEYS'])
        self.assertNotIn('OMP_NUM_THREADS',env)

if __name__=='__main__':unittest.main()
