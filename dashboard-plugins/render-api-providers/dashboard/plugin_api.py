#!/usr/bin/env python3
"""Backend API routes for the ``render-api-providers`` dashboard plugin.

The dashboard process imports this file at startup (see the ``api`` field in
``manifest.json``) and mounts the exposed ``router`` under
``/api/plugins/render-api-providers/``.

Endpoints
  GET    /custom-providers         list custom providers from config.yaml
  POST   /custom-providers         add (or update) a custom provider
  DELETE /custom-providers/{key}   remove a custom provider

Where writes land
  Custom providers live in ``config.yaml``. Current Hermes releases
  (schema v12+, including the pinned image) store them in a keyed
  ``providers:`` mapping, while older configs use a ``custom_providers:``
  list. This plugin always writes the canonical ``providers:`` entry and,
  when a matching legacy list entry exists, updates it in place so the
  runtime's deduplication never surfaces a stale duplicate. Both formats
  are read back for the listing, so restored pre-v12 configs work too.

Security
  Upstream deliberately excludes ``/api/plugins/*`` from the dashboard's
  session-token auth middleware — that is acceptable for a loopback
  dashboard, but this image serves the dashboard publicly on Render, so
  every endpoint below re-checks the session token itself and fails CLOSED
  (503) if the token helper cannot be imported. Do not remove that check.
  The listing endpoint also never returns raw inline API keys, only a
  ``has_api_key`` flag.

The pure config helpers (``*_custom_provider_entry`` and friends) take and
return plain dicts so they can be unit-tested without the hermes codebase
(see tests/test_plugin_api.py); the FastAPI routes glue them to
``hermes_cli.config.load_config`` / ``save_config``.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()

MAX_NAME_LEN = 64
MAX_KEY_LEN = 64
ENV_VAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
API_MODES = ("", "chat_completions", "anthropic_messages")


# ---------------------------------------------------------------------------
# Pure helpers (no hermes imports) — unit-testable on a plain dict
# ---------------------------------------------------------------------------


def provider_key_from_name(name: str) -> str:
    """Derive the stable provider key from a display name.

    Kebab-case, matching what the upstream v11→v12 config migration
    generates, so ``custom:<key>`` references keep working across schemas.
    Raises ValueError when the name does not yield a usable key.
    """
    key = re.sub(r"[^a-z0-9]+", "-", str(name or "").strip().lower()).strip("-")
    while "--" in key:
        key = key.replace("--", "-")
    key = key.strip("-")
    if not key:
        raise ValueError("provider name must contain at least one letter or digit")
    if len(key) > MAX_KEY_LEN:
        raise ValueError("provider name is too long")
    return key


def normalize_base_url(url: str) -> str:
    """Validate and normalize a provider endpoint URL. Raises ValueError."""
    value = str(url or "").strip()
    if not value:
        raise ValueError("base_url is required")
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("base_url must be an http(s):// URL with a host")
    return value.rstrip("/")


def normalize_api_mode(mode: str) -> str:
    value = str(mode or "").strip().lower()
    if value not in API_MODES:
        raise ValueError(
            "api_mode must be one of: chat_completions, anthropic_messages "
            "(or empty for auto-detect)"
        )
    return value


def normalize_key_env(value: str) -> str:
    name = str(value or "").strip()
    if name and not ENV_VAR_RE.match(name):
        raise ValueError("key_env must be a valid environment variable name")
    return name


def _first_str(raw: dict, *keys: str) -> str:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _normalize_entry(raw: dict, *, key: str = "", source: str) -> dict | None:
    """Reduce a raw config entry (either schema) to the API's entry shape."""
    base_url = _first_str(raw, "base_url", "url", "api")
    if not base_url:
        return None
    name = _first_str(raw, "name") or (key or "").strip()
    if not name:
        return None
    entry_key = key or provider_key_from_name(name)
    return {
        "key": entry_key,
        "name": name,
        "base_url": base_url.rstrip("/"),
        "api_mode": _first_str(raw, "api_mode", "transport"),
        "model": _first_str(raw, "model", "default_model"),
        "key_env": _first_str(raw, "key_env", "api_key_env"),
        "has_api_key": bool(_first_str(raw, "api_key")),
        "source": source,
    }


def list_custom_provider_entries(config: dict) -> list[dict]:
    """All custom providers across both schemas, deduplicated.

    The canonical ``providers:`` mapping wins; a legacy list entry is
    skipped when a canonical entry matches it by (name, base_url).
    """
    entries: list[dict] = []
    seen: set[tuple[str, str]] = set()

    providers = config.get("providers")
    if isinstance(providers, dict):
        for key, raw in providers.items():
            if not isinstance(raw, dict):
                continue
            entry = _normalize_entry(raw, key=str(key), source="providers")
            if entry is None:
                continue
            entries.append(entry)
            seen.add((entry["name"].lower(), entry["base_url"].lower()))

    legacy = config.get("custom_providers")
    if isinstance(legacy, list):
        for raw in legacy:
            if not isinstance(raw, dict):
                continue
            entry = _normalize_entry(raw, source="custom_providers")
            if entry is None:
                continue
            if (entry["name"].lower(), entry["base_url"].lower()) in seen:
                continue
            entries.append(entry)

    return entries


def current_main_provider(config: dict) -> str:
    model_cfg = config.get("model")
    if not isinstance(model_cfg, dict):
        return ""
    provider = model_cfg.get("provider")
    return str(provider).strip() if isinstance(provider, str) else ""


def provider_in_use_as_main(config: dict, key: str) -> bool:
    """True when model.provider points at this custom provider.

    Compares using the runtime's normalization (lowercase, spaces→hyphens),
    so hand-edited configs that reference the display name are covered too.
    """
    provider = current_main_provider(config)
    if not provider.lower().startswith("custom:"):
        return False
    suffix = provider.split(":", 1)[1].strip().lower().replace(" ", "-")
    return suffix == str(key or "").strip().lower()


def _legacy_entry_matches(entry: dict, name: str, key: str) -> bool:
    if not isinstance(entry, dict):
        return False
    raw_name = str(entry.get("name", "") or "").strip()
    if raw_name.lower() == name.lower():
        return True
    try:
        return provider_key_from_name(raw_name) == key
    except ValueError:
        return False


def _upsert_legacy_entry(config: dict, name: str, key: str, fields: dict) -> None:
    """Update a matching legacy custom_providers entry in place (if any)."""
    legacy = config.get("custom_providers")
    if not isinstance(legacy, list):
        return
    for entry in legacy:
        if not _legacy_entry_matches(entry, name, key):
            continue
        entry["base_url"] = fields["base_url"]
        if fields.get("api_key"):
            entry["api_key"] = fields["api_key"]
        if "api_key" in fields and not fields["api_key"]:
            entry.pop("api_key", None)
        if fields.get("key_env"):
            entry["key_env"] = fields["key_env"]
        else:
            entry.pop("key_env", None)
            entry.pop("api_key_env", None)
        if fields.get("api_mode"):
            entry["api_mode"] = fields["api_mode"]
        else:
            entry.pop("api_mode", None)
        if fields.get("model"):
            entry["model"] = fields["model"]
        else:
            entry.pop("model", None)
            entry.pop("default_model", None)


def upsert_custom_provider_entry(config: dict, fields: dict) -> str:
    """Insert or update a custom provider; returns its provider key.

    ``fields`` must contain: name, base_url; optional: api_key, key_env,
    api_mode, model, key (derived by the caller when empty).
    Raises ValueError for input problems and LookupError when the key
    already belongs to a different provider name.
    """
    name = str(fields.get("name", "") or "").strip()
    if not name:
        raise ValueError("name is required")
    if len(name) > MAX_NAME_LEN:
        raise ValueError(f"name must be at most {MAX_NAME_LEN} characters")
    key = str(fields.get("key", "") or "").strip() or provider_key_from_name(name)

    providers = config.get("providers")
    if not isinstance(providers, dict):
        providers = {}
        config["providers"] = providers

    existing = providers.get(key)
    if isinstance(existing, dict):
        existing_name = str(existing.get("name", "") or "").strip()
        if existing_name and existing_name.lower() != name.lower():
            raise LookupError(
                f"a provider with this key already exists under the name "
                f"'{existing_name}'; pick a different name"
            )
        entry = dict(existing)
    else:
        entry = {}

    entry["name"] = name
    entry["base_url"] = fields["base_url"]
    # A blank api_key on update means "keep the current key" (the form
    # cannot represent "clear" without a dedicated affordance).
    if fields.get("api_key"):
        entry["api_key"] = fields["api_key"]
    if fields.get("key_env"):
        entry["key_env"] = fields["key_env"]
        entry.pop("api_key_env", None)
    if fields.get("api_mode"):
        entry["api_mode"] = fields["api_mode"]
        entry.pop("transport", None)
    if fields.get("model"):
        entry["default_model"] = fields["model"]
        entry.pop("model", None)
    providers[key] = entry

    _upsert_legacy_entry(config, name, key, fields)
    return key


def remove_custom_provider_entry(config: dict, key: str) -> bool:
    """Remove a provider from both schemas. True when something was removed."""
    removed = False
    providers = config.get("providers")
    if isinstance(providers, dict) and key in providers:
        del providers[key]
        removed = True

    legacy = config.get("custom_providers")
    if isinstance(legacy, list):
        kept = []
        for entry in legacy:
            if _legacy_entry_matches(entry, "", key):
                removed = True
                continue
            kept.append(entry)
        config["custom_providers"] = kept
    return removed


# ---------------------------------------------------------------------------
# Request plumbing
# ---------------------------------------------------------------------------


def _require_session(request: Request) -> None:
    """Re-check the dashboard session token; fail closed on any problem.

    Upstream's auth middleware skips /api/plugins/* entirely, and this
    image runs the dashboard on a public interface — so the gate lives
    here, per route.
    """
    try:
        from hermes_cli.web_server import _has_valid_session_token
    except Exception:
        raise HTTPException(
            status_code=503,
            detail=(
                "plugin auth check unavailable; refusing to serve this "
                "route without the session-token gate"
            ),
        )
    if not _has_valid_session_token(request):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _config_backend():
    try:
        from hermes_cli import config as hermes_config
    except Exception:
        raise HTTPException(
            status_code=503, detail="hermes config backend unavailable"
        )
    return hermes_config.load_config, hermes_config.save_config


def _parse_upsert_body(body: dict) -> dict:
    if not isinstance(body, dict):
        raise ValueError("request body must be a JSON object")
    name = str(body.get("name", "") or "").strip()
    fields = {
        "name": name,
        "base_url": normalize_base_url(body.get("base_url", "")),
        "api_key": str(body.get("api_key", "") or "").strip(),
        "key_env": normalize_key_env(body.get("key_env", "")),
        "api_mode": normalize_api_mode(body.get("api_mode", "")),
        "model": str(body.get("model", "") or "").strip(),
        "key": "",
    }
    if fields["model"] and len(fields["model"]) > 200:
        raise ValueError("model must be at most 200 characters")
    return fields


def _entry_for_response(config: dict, key: str) -> dict:
    for entry in list_custom_provider_entries(config):
        if entry["key"] == key:
            return entry
    raise LookupError(f"provider '{key}' not found")


@router.get("/custom-providers")
async def list_custom_providers(request: Request):
    _require_session(request)
    load_config, _ = _config_backend()
    config = load_config()
    return {
        "providers": list_custom_provider_entries(config),
        "main_provider": current_main_provider(config),
    }


@router.post("/custom-providers")
async def upsert_custom_provider(request: Request, body: dict):
    _require_session(request)
    try:
        fields = _parse_upsert_body(body)
        key = provider_key_from_name(fields["name"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    fields["key"] = key

    load_config, save_config = _config_backend()
    config = load_config()
    try:
        upsert_custom_provider_entry(config, fields)
    except LookupError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    save_config(config)
    entry = _entry_for_response(config, key)
    return {
        "ok": True,
        "key": key,
        "provider_ref": f"custom:{key}",
        "name": entry["name"],
        "base_url": entry["base_url"],
        "note": "applies to new sessions",
    }


@router.delete("/custom-providers/{key}")
async def delete_custom_provider(request: Request, key: str):
    _require_session(request)
    load_config, save_config = _config_backend()
    config = load_config()
    if provider_in_use_as_main(config, key):
        raise HTTPException(
            status_code=409,
            detail=(
                "This provider is the current main model. Choose a different "
                "main model first (Model Settings above), then remove it."
            ),
        )
    if not remove_custom_provider_entry(config, key):
        raise HTTPException(status_code=404, detail=f"provider '{key}' not found")
    save_config(config)
    return {"ok": True}
