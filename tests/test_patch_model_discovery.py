from __future__ import annotations

import importlib.util
import unittest
import types
import textwrap
from unittest.mock import patch
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "patch-model-discovery.py"
)
_spec = importlib.util.spec_from_file_location("patch_model_discovery", MODULE_PATH)
assert _spec is not None and _spec.loader is not None
_patch_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_patch_module)


class PatchModelDiscoveryTests(unittest.TestCase):
    def test_patches_all_custom_provider_discovery_paths(self):
        source = '''
            api_key = str(ep_cfg.get("api_key", "") or "").strip()
            if not api_key:
                key_env = str(ep_cfg.get("key_env", "") or "").strip()
                api_key = os.environ.get(key_env, "").strip() if key_env else ""
            if api_url and api_key and discover:
                try:
                    from hermes_cli.models import fetch_api_models
                    live_models = fetch_api_models(api_key, api_url)
                    if live_models:
                        models_list = live_models
                except Exception:
                    pass
                    "api_url": api_url,
                    "models": [],
                }
            api_key = (entry.get("api_key") or "").strip()

            group_key = (api_url, api_key)
            if api_url and api_key:
                try:
                    from hermes_cli.models import fetch_api_models

                    live_models = fetch_api_models(api_key, api_url)
                    if live_models:
                        grp["models"] = live_models
                        grp["total_models"] = len(live_models)
                except Exception:
                    pass
'''
        patched = _patch_module.patch_model_discovery(source)
        self.assertIn("if api_url and discover:", patched)
        self.assertIn("api_mode=api_mode or None", patched)
        self.assertIn("get_env_value(key_env)", patched)
        self.assertIn("api_mode=grp.get(\"api_mode\") or None", patched)
        self.assertNotIn("if api_url and api_key and discover:", patched)
        self.assertNotIn("if api_url and api_key:\n", patched)

    def test_picker_tries_next_key_and_keeps_manual_ids(self):
        calls = []
        def fetch(key, *args, **kwargs):
            calls.append(key)
            if key == "invalid":
                raise RuntimeError("authentication failed")
            return ["discovered", "manual"]
        models = types.ModuleType("hermes_cli.models")
        models.fetch_api_models = fetch
        hermes = types.ModuleType("hermes_cli")
        hermes.models = models
        with patch.dict("sys.modules", {"hermes_cli": hermes, "hermes_cli.models": models}):
            env = {"ep_cfg": {"api_keys": ["invalid", "valid"]},
                   "api_key": "invalid", "api_url": "https://api.example/v1", "discover": True,
                   "models_list": ["manual"]}
            exec(textwrap.dedent(_patch_module._SECTION3_NEW), env)
        self.assertEqual(calls, ["invalid", "valid"])
        self.assertEqual(env["models_list"], ["manual", "discovered"])

    def test_patch_is_idempotent(self):
        source = '''
            api_key = str(ep_cfg.get("api_key", "") or "").strip()
            if not api_key:
                key_env = str(ep_cfg.get("key_env", "") or "").strip()
                api_key = os.environ.get(key_env, "").strip() if key_env else ""
            if api_url and api_key and discover:
                try:
                    from hermes_cli.models import fetch_api_models
                    live_models = fetch_api_models(api_key, api_url)
                    if live_models:
                        models_list = live_models
                except Exception:
                    pass
                    "api_url": api_url,
                    "models": [],
                }
            api_key = (entry.get("api_key") or "").strip()

            group_key = (api_url, api_key)
            if api_url and api_key:
                try:
                    from hermes_cli.models import fetch_api_models

                    live_models = fetch_api_models(api_key, api_url)
                    if live_models:
                        grp["models"] = live_models
                        grp["total_models"] = len(live_models)
                except Exception:
                    pass
'''
        patched = _patch_module.patch_model_discovery(source)
        self.assertEqual(patched, _patch_module.patch_model_discovery(patched))


if __name__ == "__main__":
    unittest.main()

