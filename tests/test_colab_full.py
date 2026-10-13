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
        self.assertEqual(env['HERMES_MAX_CONCURRENT_SESSIONS'], '10')
        self.assertEqual(env['GIT_STATE_HTTP_LOW_SPEED_LIMIT'], '0')
        self.assertEqual(env['GIT_STATE_GIT_TIMEOUT_SECONDS'], '1800')
        self.assertEqual(env['HERMES_RESTORE_TIMEOUT_SECONDS'], '1800')
        self.assertEqual(env['HERMES_COLAB_STARTUP_TIMEOUT_SECONDS'], '1800')
        self.assertIn('GIT_STATE_HTTP_LOW_SPEED_LIMIT', env['HERMES_ENV_OVERRIDE_KEYS'])
        self.assertIn('HERMES_MAX_CONCURRENT_SESSIONS', env['HERMES_ENV_OVERRIDE_KEYS'])

    def test_v2_upgrades_v1_and_preserves_higher_budgets(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            (home / '.colab-full-profile-v1').touch()
            (home / 'config.yaml').write_text('max_concurrent_sessions: 2\nagent:\n  max_turns: 30\n  gateway_timeout: 7200\nmodel:\n  default: my-model\ndelegation:\n  max_concurrent_children: 1\n')
            channel = type('Channel', (), {'install_id': staticmethod(lambda p: 'test'), '_write_channel_record': staticmethod(lambda *a, **k: True)})()
            with patch.dict('sys.modules', {'hermes_cli.update_channel': channel}):
                profile.apply(home, home / 'install')
                import yaml
                config = yaml.safe_load((home / 'config.yaml').read_text())
                self.assertEqual(config['max_concurrent_sessions'], 10)
                self.assertIsNone(config['agent']['max_turns'])
                self.assertEqual(config['agent']['gateway_timeout'], 7200)
                self.assertEqual(config['delegation']['max_concurrent_children'], 10)
                self.assertEqual(config['model']['default'], 'my-model')
                self.assertTrue((home / '.colab-full-profile-v2').exists())
                config['max_concurrent_sessions'] = 7
                (home / 'config.yaml').write_text(yaml.safe_dump(config))
                profile.apply(home, home / 'install')
                self.assertEqual(yaml.safe_load((home / 'config.yaml').read_text())['max_concurrent_sessions'], 7)

    def test_recovery_default_matches_colab_and_render(self):
        recovery = load('concurrency_recovery', 'scripts/task-recovery.py')
        with patch.dict(os.environ, {'HERMES_RUNTIME_PROFILE': 'colab-full'}, clear=True):
            self.assertEqual(recovery.session_concurrency(), 10)
            with patch.dict(os.environ, {'HERMES_MAX_CONCURRENT_SESSIONS': 'bad'}):
                self.assertEqual(recovery.session_concurrency(), 10)
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(recovery.session_concurrency(), 2)


if __name__=='__main__':unittest.main()

