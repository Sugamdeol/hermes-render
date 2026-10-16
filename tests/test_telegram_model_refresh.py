import asyncio
import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('telegram_model_patch',ROOT/'scripts/patch-telegram-models.py')
p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)
GATEWAY=(ROOT/'tests/fixtures/telegram-model-gateway.txt').read_text()
PROVIDERS=(ROOT/'tests/fixtures/telegram-model-providers.txt').read_text()


class TelegramModelRefreshTests(unittest.IsolatedAsyncioTestCase):
    def environment(self):
        ms=types.ModuleType('hermes_cli.model_switch')
        calls=[]
        def listing(**kwargs):
            calls.append(kwargs)
            ids=[f'model-{i}' for i in range(120)]
            return [{'slug':'relay','name':'Relay','is_user_defined':True,'api_url':'https://relay.test',
                     'models':ids if kwargs['max_models'] is None else ids[:kwargs['max_models']]}]
        ms.list_authenticated_providers=listing
        models=types.ModuleType('hermes_cli.models');models.fetch_openrouter_models=lambda **kwargs:[]
        validation=types.ModuleType('hermes_cli.models_validate');validation.drop_unofferable_model_ids=lambda rows:None
        providers=types.ModuleType('hermes_cli.providers');providers.get_label=lambda x:x
        ns={'asyncio':asyncio,'_prepend_moa_picker_provider':lambda rows,**kw:rows,'_cap_models':lambda ids,maximum,*args:ids if maximum is None else ids[:maximum]}
        exec('from __future__ import annotations\n'+p.replace_all(PROVIDERS,p.PROVIDER_REPLACEMENTS),ns)
        picker=types.ModuleType('hermes_cli.model_switch_providers');picker.list_picker_providers=ns['list_picker_providers']
        modules={'hermes_cli':types.ModuleType('hermes_cli'),'hermes_cli.model_switch':ms,
                 'hermes_cli.models':models,'hermes_cli.models_validate':validation,
                 'hermes_cli.providers':providers,'hermes_cli.model_switch_providers':picker}
        native={'asyncio':asyncio}
        exec('from __future__ import annotations\n'+p.replace_all(GATEWAY,p.GATEWAY_REPLACEMENTS),native)
        return native['NativeModelCommands'](),modules,calls

    async def test_telegram_receives_models_beyond_first_50(self):
        runner,modules,calls=self.environment()
        runner._thread_metadata_for_source=lambda *a:{};runner._reply_anchor_for_event=lambda e:None
        adapter=types.SimpleNamespace(send_model_picker=AsyncMock(return_value=types.SimpleNamespace(success=True)))
        with patch.dict(sys.modules,modules):
            sent=await runner._send_model_picker(object(),types.SimpleNamespace(chat_id='1'),adapter,'session',
                {'current_model':'model-1','current_provider':'relay'},AsyncMock())
        self.assertTrue(sent)
        self.assertEqual(len(adapter.send_model_picker.call_args.kwargs['providers'][0]['models']),120)
        self.assertIsNone(calls[0]['max_models'])

    async def test_refresh_reaches_native_catalog_builder_and_probes_all_endpoints(self):
        runner,modules,calls=self.environment()
        runner._delivery_adapter_for=lambda source:type('PickerAdapter',(),{'send_model_picker':lambda *a:None})()
        runner._send_model_picker=AsyncMock(return_value=True)
        ctx=types.SimpleNamespace(current_provider='relay',current_base_url='https://relay.test',current_model='model-1',
            user_provs={'relay':{}},custom_provs=[],excluded_provs=[],source=types.SimpleNamespace(),session_key='s')
        with patch.dict(sys.modules,modules):
            await runner._model_listing_reply(object(),ctx,None,force_refresh=True)
            kwargs=runner._send_model_picker.call_args.args[4]
            modules['hermes_cli.model_switch_providers'].list_picker_providers(max_models=None,**kwargs)
        self.assertTrue(calls[0]['refresh'])
        self.assertTrue(calls[0]['probe_custom_providers'])
        self.assertFalse(calls[0]['non_blocking_catalogs'])

    async def test_normal_open_retains_nonblocking_catalogs(self):
        runner,modules,calls=self.environment()
        runner._delivery_adapter_for=lambda source:type('PickerAdapter',(),{'send_model_picker':lambda *a:None})()
        runner._send_model_picker=AsyncMock(return_value=True)
        ctx=types.SimpleNamespace(current_provider='relay',current_base_url='',current_model='m',user_provs={},
            custom_provs=[],excluded_provs=[],source=None,session_key='s')
        with patch.dict(sys.modules,modules):await runner._model_listing_reply(object(),ctx,None)
        kwargs=runner._send_model_picker.call_args.args[4]
        self.assertTrue(kwargs['non_blocking_catalogs']);self.assertFalse(kwargs['refresh'])
        self.assertFalse(kwargs['probe_custom_providers'])

    def test_command_forwards_refresh_flag_and_patch_is_idempotent(self):
        updated=p.replace_all(GATEWAY,p.GATEWAY_REPLACEMENTS)
        self.assertIn('force_refresh=request.force_refresh',updated)
        self.assertEqual(p.replace_all(updated,p.GATEWAY_REPLACEMENTS),updated)
        updated=p.replace_all(PROVIDERS,p.PROVIDER_REPLACEMENTS)
        self.assertEqual(p.replace_all(updated,p.PROVIDER_REPLACEMENTS),updated)
        with self.assertRaises(ValueError):p.replace_all('changed upstream',p.GATEWAY_REPLACEMENTS)

    def test_incompatible_second_file_leaves_first_file_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'gateway').mkdir();(root/'hermes_cli').mkdir()
            gateway=root/'gateway/slash_commands_model.py';gateway.write_text(GATEWAY)
            (root/'hermes_cli/model_switch_providers.py').write_text('changed upstream')
            with self.assertRaises(ValueError):p.patch(root)
            self.assertEqual(gateway.read_text(),GATEWAY)
