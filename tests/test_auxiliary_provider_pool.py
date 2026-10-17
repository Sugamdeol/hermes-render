import ast
import importlib.util
import os
from pathlib import Path
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('dashboard_aux_pool', ROOT / 'scripts/auxiliary-provider-pool.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class AuxiliaryPoolTests(unittest.TestCase):
    def setUp(self):
        self.entry = {'name': 'Fast Router', 'provider_key': 'fast-router', 'base_url': 'https://api.example/v1', 'api_key': 'empty-key', 'api_keys': ['empty-key', 'working-key']}
        self.pool = Mock()
        self.pool.has_credentials.return_value = True
        self.pool.select.return_value = types.SimpleNamespace(runtime_api_key='working-key')
        self.provider = types.ModuleType('hermes_cli.runtime_provider')
        self.provider._get_named_custom_provider = Mock(return_value=self.entry)
        self.credentials = types.ModuleType('agent.credential_pool')
        self.credentials.custom_provider_pool_key_candidates = Mock(return_value=['fast-router'])
        self.credentials.load_pool = Mock(return_value=self.pool)
        self.modules = patch.dict('sys.modules', {'hermes_cli.runtime_provider': self.provider, 'agent.credential_pool': self.credentials, 'agent.dashboard_aux_pool': helper})
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def test_rotated_key_wins_over_first_inline_key(self):
        binding = helper.configured_pool('fast-router', 'https://api.example/v1/')
        self.assertEqual(helper.selected_key(binding), 'working-key')
        self.credentials.custom_provider_pool_key_candidates.assert_called_once_with('https://api.example/v1', 'fast-router')

    def test_exhausted_pool_does_not_reuse_bad_inline_key(self):
        self.pool.select.return_value = None
        with self.assertRaisesRegex(RuntimeError, 'All configured keys'):
            helper.selected_key(helper.configured_pool('fast-router', self.entry['base_url']))

    def test_endpoint_overrides_never_receive_pool_keys(self):
        for endpoint in ('https://other.example/v1', 'http://api.example/v1', 'https://api.example/other'):
            self.assertIsNone(helper.configured_pool('fast-router', endpoint))
        self.credentials.load_pool.assert_not_called()

    def test_single_key_and_command_credentials_keep_native_resolution(self):
        for change in ({'api_keys': None}, {'key_cmd': 'fetch-token'}):
            with patch.dict(self.entry, change):
                self.assertIsNone(helper.configured_pool('fast-router', self.entry['base_url']))

    def test_other_providers_are_not_discovered(self):
        self.provider._get_named_custom_provider.return_value = None
        self.assertIsNone(helper.configured_pool('unrelated', 'https://unrelated/v1'))
        self.credentials.load_pool.assert_not_called()

    def upstream_function(self, name, namespace):
        source = os.environ.get('HERMES_TEST_UPSTREAM')
        if not source:
            self.skipTest('Set HERMES_TEST_UPSTREAM to test patched upstream functions')
        tree = ast.parse((Path(source) / 'agent/auxiliary_client.py').read_text())
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
        module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), node], type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), '<patched upstream>', 'exec'), namespace)
        return namespace[name]

    def test_patched_named_resolver_uses_rotated_pool_key(self):
        resolver = self.upstream_function('_named_custom_api_key', {})
        self.assertEqual(resolver(self.entry, 'fast-router', self.entry['base_url']), 'working-key')

    def test_patched_main_route_uses_pool_despite_old_provider_cooldown(self):
        resolve = Mock(return_value=(object(), 'my-model'))
        namespace = {'_custom_health_base_url': lambda p, b: b, '_normalize_chain_label': lambda p: p, '_is_provider_unhealthy': Mock(return_value=True), 'resolve_provider_client': resolve, 'logger': Mock()}
        route = self.upstream_function('_try_main_provider_route', namespace)
        self.assertIsNotNone(route('fast-router', 'my-model', self.entry['base_url'], 'empty-key', ''))
        self.assertEqual(resolve.call_args.kwargs['explicit_api_key'], 'working-key')
        namespace['_is_provider_unhealthy'].assert_not_called()

    def test_recovery_marks_exact_failed_key_and_evicts_auto_cache(self):
        evict = Mock()
        self.pool.mark_exhausted_and_rotate.return_value = object()
        namespace = {'_normalize_aux_provider': lambda p: p, 'load_pool': self.credentials.load_pool, '_is_auth_error': lambda e: False, '_is_payment_error': lambda e: True, '_evict_cached_clients': evict, 'logger': Mock()}
        recover = self.upstream_function('_recover_provider_pool', namespace)
        error = RuntimeError('credit exceeded')
        error.status_code = 402
        self.assertTrue(recover('fast-router', error, failed_api_key='empty-key'))
        self.assertEqual(self.pool.mark_exhausted_and_rotate.call_args.kwargs['api_key_hint'], 'empty-key')
        self.assertEqual([c.args[0] for c in evict.call_args_list], ['fast-router', 'auto'])


if __name__ == '__main__':
    unittest.main()
