import ast
import os
import asyncio
import logging
import runpy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional

PATCH = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/patch-topic-models.py"))["patch"]

@unittest.skipUnless(os.environ.get("HERMES_AGENT_ROOT"), "Set HERMES_AGENT_ROOT to the pinned upstream checkout")
class TopicPickerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        source = (Path(os.environ["HERMES_AGENT_ROOT"]) / "gateway/platforms/telegram.py").read_text()
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        target = root / "gateway/platforms/telegram.py"
        target.parent.mkdir(parents=True)
        target.write_text(source)
        PATCH(root)
        patched = target.read_text()
        PATCH(root)
        self.assertEqual(patched, target.read_text())
        tree = ast.parse(patched)
        names = {"send_model_picker", "_handle_model_picker_callback", "_build_model_keyboard"}
        methods = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
        cls = ast.ClassDef(name="Adapter", bases=[], keywords=[], body=methods, decorator_list=[])
        ns = dict(Optional=Optional, Dict=Dict, Any=Any, SendResult=lambda **kw: SimpleNamespace(**kw),
                  InlineKeyboardButton=lambda *a, **kw: None, InlineKeyboardMarkup=lambda rows: rows,
                  ParseMode=SimpleNamespace(MARKDOWN="Markdown"), logger=logging.getLogger())
        exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])), "<adapter>", "exec"), ns)
        self.adapter = ns["Adapter"]()
        self.adapter._model_picker_state = {}
        self.adapter._MODEL_PAGE_SIZE = 8
        self.adapter._link_preview_kwargs = lambda: {}
        self.counter = 0
        async def send(**kw):
            self.counter += 1
            return SimpleNamespace(message_id=self.counter)
        self.adapter._bot = SimpleNamespace(send_message=send)
        self.changes = {}
        self.providers = [{"slug": "custom", "name": "Custom", "models": ["model-a", "model-b"]}]
    def tearDown(self):
        self.tmp.cleanup()
    async def picker(self, topic):
        async def selected(chat, model, provider):
            self.changes[topic] = (model, provider)
            return "Switched"
        result = await self.adapter.send_model_picker("123", self.providers, "", "", topic, selected, {"thread_id": topic})
        self.assertTrue(result.success)
        return int(result.message_id)
    def query(self, message_id):
        async def noop(**kw): pass
        return SimpleNamespace(message=SimpleNamespace(message_id=message_id), answer=noop, edit_message_text=noop)
    async def test_two_topics_select_independently(self):
        a, b = await self.picker("10"), await self.picker("20")
        await self.adapter._handle_model_picker_callback(self.query(a), "mp:custom", "123")
        await self.adapter._handle_model_picker_callback(self.query(b), "mp:custom", "123")
        await self.adapter._handle_model_picker_callback(self.query(a), "mm:0", "123")
        self.assertEqual(self.changes, {"10": ("model-a", "custom")})
        self.assertIn(("123", b), self.adapter._model_picker_state)
        await self.adapter._handle_model_picker_callback(self.query(b), "mm:1", "123")
        self.assertEqual(self.changes["20"], ("model-b", "custom"))
    async def test_cancel_and_stale_callback_do_not_touch_other_topic(self):
        a, b = await self.picker("10"), await self.picker("20")
        await self.adapter._handle_model_picker_callback(self.query(a), "mx", "123")
        await self.adapter._handle_model_picker_callback(self.query(a), "mp:custom", "123")
        await self.adapter._handle_model_picker_callback(self.query(a), "mm:0", "123")
        self.assertFalse(self.changes)
        self.assertIn(("123", b), self.adapter._model_picker_state)
    async def test_two_menus_in_same_topic_keep_original_callbacks(self):
        a, b = await self.picker("10"), await self.picker("10")
        await self.adapter._handle_model_picker_callback(self.query(a), "mp:custom", "123")
        await self.adapter._handle_model_picker_callback(self.query(a), "mm:0", "123")
        self.assertIn(("123", b), self.adapter._model_picker_state)

if __name__ == "__main__":
    unittest.main()
