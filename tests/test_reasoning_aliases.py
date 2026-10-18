import ast
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('reasoning_patch', ROOT / 'scripts/patch-reasoning-aliases.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class ReasoningAliasesTests(unittest.TestCase):
    def test_patch_is_idempotent_and_preserves_mode(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'agent/message_sanitization.py'
            path.parent.mkdir()
            path.write_text('providers = ' + helper.OLD + '\n')
            path.chmod(0o640)
            self.assertTrue(helper.patch(root))
            self.assertFalse(helper.patch(root))
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)

    def test_unknown_source_is_not_modified(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'agent/message_sanitization.py'
            path.parent.mkdir()
            path.write_text('changed_upstream = True\n')
            with self.assertRaises(ValueError):
                helper.patch(root)
            self.assertEqual(path.read_text(), 'changed_upstream = True\n')

    def test_real_upstream_replay_uses_one_alias_without_changing_history(self):
        source = os.environ.get('HERMES_TEST_UPSTREAM')
        if not source:
            self.skipTest('Set HERMES_TEST_UPSTREAM for real upstream replay verification')
        text = (Path(source) / 'agent/message_sanitization.py').read_text()
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'agent/message_sanitization.py'
            path.parent.mkdir()
            path.write_text(text)
            helper.patch(root)
            tree = ast.parse(path.read_text())
        constants = {'RC', 'R', 'RD', 'REASONING_CARRIERS', '_ALL_CARRIERS',
                     '_DEFAULT_CARRIERS', '_LOCAL_CARRIERS', '_REASONING_CARRIER_ROUTES'}
        functions = {'route_reasoning_carriers', '_replayable_reasoning_text',
                     '_apply_legacy_must_echo_pad', 'apply_reasoning_content_policy'}
        nodes = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in functions:
                nodes.append(node)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = {n.id for target in targets for n in ast.walk(target) if isinstance(n, ast.Name)}
                if names & constants:
                    nodes.append(node)
        namespace = {'Any': object, '_profile_declares_native_details': lambda provider: False}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), '<upstream reasoning>', 'exec'), namespace)
        utils = types.ModuleType('utils')
        utils.base_url_host_matches = lambda url, host: (url or '').split('/')[2].split(':')[0] == host
        metadata = types.ModuleType('agent.model_metadata')
        metadata.is_local_endpoint = lambda url: 'localhost' in url or '127.0.0.1' in url
        with patch.dict('sys.modules', {'utils': utils, 'agent.model_metadata': metadata}):
            route = namespace['route_reasoning_carriers']
            carriers = route('custom', 'https://provider.example/v1')
            self.assertEqual(carriers, frozenset({'reasoning_content'}))
            for reasoning in ({'reasoning': 'think'}, {'reasoning_content': 'think'},
                              {'reasoning': 'think', 'reasoning_content': 'think'}, {}):
                history = {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'call_1'}], **reasoning}
                original = dict(history)
                wire = dict(history)
                namespace['apply_reasoning_content_policy'](history, wire, True, carriers)
                payload = json.loads(json.dumps(wire))
                # A serde schema mapping both names to one field accepts only one carrier.
                self.assertEqual(sum(k in payload for k in ('reasoning', 'reasoning_content')), 1)
                self.assertEqual(payload['reasoning_content'], 'think' if reasoning else ' ')
                self.assertEqual(history, original)
            self.assertEqual(route('custom', 'http://localhost:11434/v1'), frozenset({'reasoning', 'reasoning_content'}))
            self.assertEqual(route('ollama-cloud', 'https://ollama.com/v1'), frozenset({'reasoning', 'reasoning_content'}))
            self.assertIn('reasoning_details', route('custom', 'https://openrouter.ai/v1'))


if __name__ == '__main__':
    unittest.main()
