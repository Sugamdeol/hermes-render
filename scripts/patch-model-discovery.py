#!/usr/bin/env python3
"""Patch the pinned Hermes picker to discover models for every custom endpoint.

The upstream release used by this image only called ``/models`` for a custom
provider when an inline ``api_key`` was present.  That skips providers using
``key_env`` and keyless local/proxy endpoints, and it sent the wrong header to
Anthropic-compatible endpoints.  Keep this small compatibility patch separate
from the dashboard plugin so upgrading the pinned image has one obvious,
failing-closed place to update.
"""
from __future__ import annotations

import sys
from pathlib import Path


_SECTION3_OLD = '''            if api_url and api_key and discover:
                try:
                    from hermes_cli.models import fetch_api_models
                    live_models = fetch_api_models(api_key, api_url)
                    if live_models:
                        models_list = live_models
                except Exception:
                    pass
'''

_SECTION3_KEY_OLD = '''            api_key = str(ep_cfg.get("api_key", "") or "").strip()
            if not api_key:
                key_env = str(ep_cfg.get("key_env", "") or "").strip()
                api_key = os.environ.get(key_env, "").strip() if key_env else ""
'''

_SECTION3_KEY_NEW = '''            api_key = str(ep_cfg.get("api_key", "") or "").strip()
            if not api_key:
                key_env = str(
                    ep_cfg.get("key_env", "") or ep_cfg.get("api_key_env", "") or ""
                ).strip()
                api_key = os.environ.get(key_env, "").strip() if key_env else ""
                if not api_key and key_env:
                    try:
                        from hermes_cli.config import get_env_value
                        api_key = str(get_env_value(key_env) or "").strip()
                    except Exception:
                        pass
'''

_SECTION3_NEW = '''            # Discover from every custom endpoint, including keyless
            # local servers.  ``fetch_api_models`` knows how to omit auth
            # headers when api_key is empty and how to use Anthropic's
            # x-api-key header when api_mode requests that transport.
            api_mode = str(
                ep_cfg.get("api_mode", "") or ep_cfg.get("transport", "") or ""
            ).strip().lower()
            if api_url and discover:
                try:
                    from hermes_cli.models import fetch_api_models
                    live_models = fetch_api_models(
                        api_key, api_url, api_mode=api_mode or None
                    )
                    if live_models:
                        models_list = live_models
                except Exception:
                    pass
'''

_SECTION4_KEY_OLD = '''            api_key = (entry.get("api_key") or "").strip()

            group_key = (api_url, api_key)
'''

_SECTION4_KEY_NEW = '''            # Resolve key_env as well as inline keys.  A missing key is
            # intentional for many local OpenAI-compatible servers, so it
            # must not prevent a /models probe.
            raw_api_key = (entry.get("api_key") or "").strip()
            key_env = str(
                entry.get("key_env", "") or entry.get("api_key_env", "") or ""
            ).strip()
            api_key = raw_api_key or os.environ.get(key_env, "").strip()
            if not api_key and key_env:
                try:
                    from hermes_cli.config import get_env_value
                    api_key = str(get_env_value(key_env) or "").strip()
                except Exception:
                    pass
            api_mode = str(
                entry.get("api_mode", "") or entry.get("transport", "") or ""
            ).strip().lower()
            discover = entry.get("discover_models", True)
            if isinstance(discover, str):
                discover = discover.lower() not in ("false", "no", "0")

            group_key = (api_url, api_key)
'''

_GROUP_OLD = '''                    "api_url": api_url,
                    "models": [],
                }
'''

_GROUP_NEW = '''                    "api_url": api_url,
                    "models": [],
                    "api_mode": api_mode,
                    "discover": discover,
                }
'''

_SECTION4_FETCH_OLD = '''            if api_url and api_key:
                try:
                    from hermes_cli.models import fetch_api_models

                    live_models = fetch_api_models(api_key, api_url)
                    if live_models:
                        grp["models"] = live_models
                        grp["total_models"] = len(live_models)
                except Exception:
                    pass
'''

_SECTION4_FETCH_NEW = '''            if api_url and grp.get("discover", True):
                try:
                    from hermes_cli.models import fetch_api_models

                    live_models = fetch_api_models(
                        api_key,
                        api_url,
                        api_mode=grp.get("api_mode") or None,
                    )
                    if live_models:
                        grp["models"] = live_models
                        grp["total_models"] = len(live_models)
                except Exception:
                    pass
'''


def patch_model_discovery(text: str) -> str:
    """Return *text* with the custom-provider discovery fixes applied.

    The replacements are deliberately exact.  If the pinned upstream source
    changes, the image build fails instead of silently shipping the regression
    again.  Re-running against an already-patched file is safe.
    """
    replacements = (
        (_SECTION3_KEY_OLD, _SECTION3_KEY_NEW, "section 3 key resolution"),
        (_SECTION3_OLD, _SECTION3_NEW, "section 3 custom-provider discovery"),
        (_SECTION4_KEY_OLD, _SECTION4_KEY_NEW, "legacy key resolution"),
        (_GROUP_OLD, _GROUP_NEW, "legacy provider discovery metadata"),
        (_SECTION4_FETCH_OLD, _SECTION4_FETCH_NEW, "legacy custom-provider discovery"),
    )
    for old, new, label in replacements:
        if old in text:
            text = text.replace(old, new, 1)
        elif new not in text:
            raise ValueError(f"expected Hermes source for {label} was not found")
    return text


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print(f"usage: {Path(sys.argv[0]).name} <hermes_cli/model_switch.py>", file=sys.stderr)
        return 2
    path = Path(args[0])
    text = path.read_text(encoding="utf-8")
    patched = patch_model_discovery(text)
    if patched != text:
        path.write_text(patched, encoding="utf-8")
        print(f"[render-tools] patched Hermes custom-provider model discovery in {path}")
    else:
        print(f"[render-tools] Hermes custom-provider model discovery already patched in {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
