import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

from test_plugin_api import load_plugin_api

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('custom_patch', ROOT / 'scripts/patch-custom-models.py')
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)
SOURCE = (ROOT / 'tests/fixtures/custom-model-picker.txt').read_text()


class Builder:
    def __init__(self, discovered, empty=False):
        self.discovered = discovered
        self.empty = empty
        self.seen_slugs = set()
        self.section3_pairs = set()
        self.builtin_endpoints = set()
        self.current_base_url_norm = ''
        self.rows = []
    def endpoint_is_current(self, *args, **kwargs):
        return False
    def discover_endpoint(self, *args, **kwargs):
        return self.discovered, self.empty, True
    def add_endpoint_row(self, slug, name, url, models, current, empty):
        self.rows.append({'slug': slug, 'models': list(models), 'empty': empty})
    def record_section3_pair(self, name, url):
        self.section3_pairs.add((name, url))
        return True


def environment(source):
    model_switch = types.ModuleType('hermes_cli.model_switch')
    model_switch._declared_model_ids = lambda models: list(models or {})
    model_switch._entry_models_discovered = lambda entry: entry.get('models_discovered') is True
    model_switch._models_config_is_allowlist = lambda models, discovered: isinstance(models, list) and not discovered
    model_switch._extra_headers_from_config = lambda entry: entry.get('extra_headers', {})
    model_switch._scoped_key_env = lambda key: 'key' if key else ''
    config = types.ModuleType('hermes_cli.config')
    config.coerce_provider_id = lambda name: str(name or '').lower()
    config.is_provider_enabled = lambda entry: entry.get('enabled', True)
    namespace = {
        '_split_chain_entries': lambda models: None,
        '_entry_base_url': lambda entry, keys=('base_url', 'api', 'url'): next((entry[k] for k in keys if entry.get(k)), ''),
        '_entry_credentials': lambda entry, *keys: (entry.get('api_key', ''), '', entry.get('api_key', '')),
        '_entry_api_mode': lambda entry: entry.get('api_mode'),
        '_norm_url': lambda value: value.rstrip('/').lower(),
        '_discover_flag': lambda entry: entry.get('discover_models', True),
        '_group_display_name': lambda name: name,
        '_display_prefix': lambda name: name,
        'custom_provider_aliases': lambda name, key: [name, key],
        'custom_provider_slug': lambda name, key='': 'custom:' + (key or name),
        'base_url_host_matches': lambda *args: False,
        '_save_discovered_models_to_config': lambda *args, **kwargs: None,
    }
    exec('from __future__ import annotations\n' + source, namespace)
    return namespace, {'hermes_cli.model_switch': model_switch, 'hermes_cli.config': config}


class CustomPickerTests(unittest.TestCase):
    def run_picker(self, source, entry, discovered, legacy=False, empty=False):
        namespace, modules = environment(source)
        builder = Builder(discovered, empty)
        with patch.dict(sys.modules, modules):
            if legacy:
                namespace['_lap_custom_provider_rows'](builder, [entry])
            else:
                namespace['_lap_user_provider_rows'](builder, {'relay': entry})
        return builder.rows[0]

    def test_reproduces_manual_id_disappearing_and_repairs_both_schemas(self):
        entry = {'name': 'relay', 'base_url': 'https://relay.example/v1', 'api_key': 'key',
                 'models': {'manual-only': {}, 'advertised': {}}}
        for legacy in (False, True):
            before = self.run_picker(SOURCE, entry, ['advertised'], legacy)
            self.assertNotIn('manual-only', before['models'])
            after = self.run_picker(adapter.patch_text(SOURCE), entry, ['advertised', 'new'], legacy)
            self.assertEqual(after['models'], ['manual-only', 'advertised', 'new'])

    def test_cached_discovery_does_not_keep_removed_auto_discovered_ids(self):
        entry = {'name': 'relay', 'base_url': 'https://relay.example/v1',
                 'models': {'old-discovered': {}}, 'models_discovered': True}
        for legacy in (False, True):
            row = self.run_picker(adapter.patch_text(SOURCE), entry, ['new-discovered'], legacy)
            self.assertEqual(row['models'], ['new-discovered'])

    def test_empty_or_unavailable_discovery_keeps_manual_ids(self):
        entry = {'name': 'relay', 'base_url': 'https://relay.example/v1', 'models': {'manual-only': {}}}
        for discovered, empty in (([], True), (None, False)):
            for legacy in (False, True):
                row = self.run_picker(adapter.patch_text(SOURCE), entry, discovered, legacy, empty)
                self.assertEqual(row['models'], ['manual-only'])
                self.assertFalse(row['empty'])

    def test_patch_is_idempotent_and_source_drift_is_visible(self):
        updated = adapter.patch_text(SOURCE)
        self.assertEqual(adapter.patch_text(updated), updated)
        with self.assertRaises(ValueError):
            adapter.patch_text('unexpected source')

    def test_saving_manual_ids_clears_discovery_ownership_and_keeps_metadata(self):
        api = load_plugin_api({})
        config = {'providers': {'relay': {'name': 'Relay', 'base_url': 'https://relay.example',
                                        'models_discovered': True, 'models': {'manual': {'context_length': 32000}}}},
                  'custom_providers': [{'name': 'Relay', 'base_url': 'https://relay.example', 'models_discovered': True}]}
        api.upsert_custom_provider_entry(config, {'key': 'relay', 'name': 'Relay', 'base_url': 'https://relay.example', 'models': ['manual']})
        self.assertNotIn('models_discovered', config['providers']['relay'])
        self.assertNotIn('models_discovered', config['custom_providers'][0])
        self.assertEqual(config['providers']['relay']['models']['manual']['context_length'], 32000)

    def test_internal_catalog_sentinels_are_not_model_ids(self):
        api = load_plugin_api({})
        self.assertEqual(api._configured_model_ids({'models': {'__discovered_model_catalog__': True, 'manual': {}}}), ['manual'])
