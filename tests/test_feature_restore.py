import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('restore_features', ROOT/'scripts/restore-colab-features.py')
features = importlib.util.module_from_spec(spec); spec.loader.exec_module(features)


class FeatureRestoreTests(unittest.TestCase):
    def test_restore_known_features_preserves_models_and_unrelated_disabled_plugins(self):
        config = {'model':{'default':'custom-model'}, 'providers':{'my-api':{'api_keys':['a','b'],'models':{'manual':{}}}},
                  'plugins':{'enabled':['existing'],'disabled':['render-api-providers','disabled-custom']},
                  'dashboard':{'hidden_plugins':['hermes-chat-dashboard','hidden-custom']}}
        features.restore_config(config, ['render-api-providers','hermes-chat-dashboard','debug-master','disabled-custom'])
        self.assertIn('render-api-providers',config['plugins']['enabled'])
        self.assertIn('hermes-chat-dashboard',config['plugins']['enabled'])
        self.assertIn('debug-master',config['plugins']['enabled'])
        self.assertNotIn('disabled-custom',config['plugins']['enabled'])
        self.assertEqual(config['plugins']['disabled'],['disabled-custom'])
        self.assertEqual(config['dashboard']['hidden_plugins'],['hidden-custom'])
        self.assertEqual(config['providers']['my-api']['api_keys'],['a','b'])
        self.assertEqual(config['model']['default'],'custom-model')
        self.assertNotIn('agent',config)
    def test_migration_runs_once_and_preserves_later_user_disable(self):
        with tempfile.TemporaryDirectory() as td:
            home=Path(td); (home/'config.yaml').write_text('{}')
            for name in features.MANAGED:
                d=home/'plugins'/name/'dashboard';d.mkdir(parents=True)
                (d/'index.js').write_text('void 0;')
                (d/'manifest.json').write_text(json.dumps({'name':name,'entry':'index.js'}))
            features.restore(home)
            config=yaml.safe_load((home/'config.yaml').read_text())
            self.assertEqual(set(config['plugins']['enabled']),features.MANAGED)
            config['plugins']['disabled']=['hermes-chat-dashboard']
            (home/'config.yaml').write_text(yaml.safe_dump(config))
            before=(home/'config.yaml').read_text();features.restore(home)
            self.assertEqual(before,(home/'config.yaml').read_text())
    def test_stale_manifest_with_one_bundle_is_restorable(self):
        with tempfile.TemporaryDirectory() as td:
            d=Path(td)/'plugins'/'debug-master'/'dashboard'
            (d/'bundle').mkdir(parents=True)
            (d/'bundle'/'index.js').write_text('void 0;')
            (d/'manifest.json').write_text(json.dumps({'name':'debug-master','entry':'dist/index.js'}))
            self.assertEqual(features.installed_dashboards(td), ['debug-master'])
    def test_missing_bundle_never_marks_migration_complete(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(RuntimeError):features.restore(td)
            self.assertFalse((Path(td)/'.colab-dashboard-features-v1').exists())

if __name__=='__main__':unittest.main()
