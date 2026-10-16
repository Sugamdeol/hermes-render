import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('live_model_fix',ROOT/'hotfix-telegram-models.py')
h=importlib.util.module_from_spec(spec);spec.loader.exec_module(h)


class LiveModelFixTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.install=Path(t.name)
        (self.install/'gateway').mkdir();(self.install/'hermes_cli').mkdir()
        (self.install/'gateway/slash_commands_model.py').write_text('''class GatewayModelCommandsMixin:
    async def _send_model_picker(self): return marker
    async def _model_listing_reply(self, *, force_refresh=False): return force_refresh
    async def _handle_model_command_locked(self): return self.session
''')
        (self.install/'hermes_cli/model_switch_providers.py').write_text('def list_picker_providers(refresh=False): return marker, refresh\n')
        class ModelCommands:
            async def _send_model_picker(self): return 'old'
            async def _model_listing_reply(self): return 'old'
            async def _handle_model_command_locked(self): return 'old'
        self.cls=ModelCommands
        self.gateway=types.ModuleType('gateway.slash_commands_model');self.gateway.GatewayModelCommandsMixin=self.cls
        self.gateway.marker=object()
        self.providers=types.ModuleType('hermes_cli.model_switch_providers');self.providers.marker=object()
        self.providers.list_picker_providers=lambda: 'old'
        self.modules={'gateway.slash_commands_model':self.gateway,'gateway.run':types.ModuleType('gateway.run'),
                      'hermes_cli.model_switch_providers':self.providers}

    def test_existing_gateway_instance_and_session_survive_live_repair(self):
        import asyncio
        runner=self.cls();runner.session=object();session=runner.session
        with patch.dict(sys.modules,self.modules):proof=h.apply_live(self.install)
        self.assertEqual(proof['pid'],os.getpid());self.assertEqual(proof['updated_functions'],4)
        self.assertIs(asyncio.run(runner._handle_model_command_locked()),session)
        self.assertIs(asyncio.run(runner._send_model_picker()),self.gateway.marker)
        self.assertTrue(asyncio.run(runner._model_listing_reply(force_refresh=True)))
        self.assertEqual(self.providers.list_picker_providers(refresh=True),(self.providers.marker,True))

    def test_compilation_failure_leaves_all_live_functions_untouched(self):
        old=self.cls._send_model_picker;old_listing=self.providers.list_picker_providers
        (self.install/'hermes_cli/model_switch_providers.py').write_text('def unrelated(): pass\n')
        with patch.dict(sys.modules,self.modules),self.assertRaises(RuntimeError):h.apply_live(self.install)
        self.assertIs(self.cls._send_model_picker,old)
        self.assertIs(self.providers.list_picker_providers,old_listing)

    def test_cli_discovery_does_not_claim_to_patch_the_gateway(self):
        with patch.dict(sys.modules,self.modules):
            del sys.modules['gateway.run']
            self.assertIsNone(h.apply_live(self.install))

    def test_live_repair_is_repeatable_without_replacing_the_session(self):
        runner=self.cls();runner.session=object();before=runner.session
        with patch.dict(sys.modules,self.modules):
            h.apply_live(self.install);h.apply_live(self.install)
        self.assertIs(runner.session,before)

    def test_only_named_methods_compile_not_module_side_effects(self):
        namespace={'marker':'kept'}
        functions=h.compile_functions('raise RuntimeError("must not run")\ndef chosen(): return marker\n',('chosen',),namespace)
        self.assertEqual(functions['chosen'](),'kept')
        self.assertNotIn('chosen',namespace)
