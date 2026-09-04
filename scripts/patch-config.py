#!/opt/hermes/.venv/bin/python
"""Idempotent patcher for Hermes' ~/.hermes/config.yaml on Render.

Adds three things the first time it runs against a given config.yaml:
  1. mcp_servers.render -- HTTP MCP server pointed at mcp.render.com,
     authenticated via the RENDER_MCP_API_KEY env var. Hermes supports
     ${VAR} substitution in headers, so the key is resolved lazily at
     gateway startup. Users can rotate the key from Render's Environment
     tab without rebuilding the image.

     The Render MCP server is registered without a `tools.include`
     filter, so Hermes can see every MCP tool the provided API key is
     allowed to use. Operators should treat this as full Render account
     access and secure the dashboard/API key accordingly.

  2. custom_providers.bynara -- the Bynara OpenAI-compatible router, using
     BYNARA_API_KEY from the process environment. When that key is present
     and the config still has the upstream default model, the patcher
     selects Bynara's free qwen-3.8-max-free model without replacing an
     explicit user model choice.

  3. skills.external_dirs -- exposes two pre-baked skill bundles to
     skills_list() and the / slash command surface, without colliding
     with the upstream skills_sync flow on /opt/data/skills:
       - /opt/render-tools/skills-local    (Hermes-on-Render overlay)
       - /opt/render-tools/skills-upstream (pinned render-oss/skills)
     The local overlay is listed first so its skill names win on collision.

  4. Conservative Free-tier resource defaults -- reduce turn/retry budgets,
     delegation and auxiliary concurrency, compression pressure, browser
     lifetimes, and tool-output sizes. Existing values are never changed.

Integration patching is INSERT-only by design. If an existing provider or
MCP entry already exists (even pointing somewhere different), it leaves that
entry alone. Free resource values are lowered once when the config is first
created or migrated from the pinned upstream template; a marker then keeps
restored or edited values alone on later boots. The Bynara default is only
selected when BYNARA_API_KEY is present and
the model still has the upstream default, so an explicit model choice wins.
This means:
  - Re-running the patcher on every boot is safe.
  - Users who edit config.yaml from the dashboard own those edits.
  - The skill bundle in the image always loads at /opt/render-tools/skills,
    regardless of whether external_dirs has other entries.

Uses PyYAML, which ships with Hermes' .venv.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml

# Listed in precedence order: skills-local wins on name collision with
# skills-upstream, which lets our overlay shadow a same-named upstream skill.
RENDER_SKILL_DIRS = (
    "/opt/render-tools/skills-local",
    "/opt/render-tools/skills-upstream",
)
RENDER_MCP_URL = "https://mcp.render.com/mcp"
RENDER_MCP_AUTH = "Bearer ${RENDER_MCP_API_KEY}"
# Seconds the MCP client waits for the initial connection. Upstream's default
# is 60 (_DEFAULT_CONNECT_TIMEOUT in tools/mcp_tool.py), and
# tui_gateway/entry.py calls discover_mcp_tools() SYNCHRONOUSLY before it
# emits `gateway.ready` -- the event the browser Chat tab waits on. So an
# unreachable or slow mcp.render.com at boot leaves the dashboard's chat
# unusable for the whole timeout.
#
# Measured against a non-routable address: discover_mcp_tools() blocked
# 60.05 s at the default and 5.03 s with a 5 s bound, returning [] either way.
# 10 s is generous for a healthy TLS + MCP initialize (a TLS round trip to
# GitHub measured 0.55-0.61 s wall on this 0.1 CPU budget) while capping the
# worst case at a sixth of upstream's. This bounds how long boot waits; it
# does not disable the server -- the tools still register once it answers,
# and discover_mcp_tools() retries the missing servers on its next call.
RENDER_MCP_CONNECT_TIMEOUT_ENV = "HERMES_RENDER_MCP_CONNECT_TIMEOUT"
RENDER_MCP_CONNECT_TIMEOUT_DEFAULT = 10.0
BYNARA_BASE_URL = "https://router.bynara.id/v1"
BYNARA_API_KEY_ENV = "BYNARA_API_KEY"
BYNARA_DEFAULT_MODEL = "qwen-3.8-max-free"


def load_config(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"[render-tools] cannot read {path}: {exc}", file=sys.stderr)
        return {}
    if not raw.strip():
        return {}
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        print(
            f"[render-tools] {path} is not valid YAML ({exc}); refusing to patch",
            file=sys.stderr,
        )
        sys.exit(1)
    return data if isinstance(data, dict) else {}


def _mcp_connect_timeout() -> float:
    """Read the MCP connect timeout from the environment, falling back safely.

    An unparsable value falls back to the default rather than raising: this
    runs at boot, and a typo in one environment variable should not stop the
    container from starting.
    """
    raw = os.environ.get(RENDER_MCP_CONNECT_TIMEOUT_ENV)
    if raw is None or not str(raw).strip():
        return RENDER_MCP_CONNECT_TIMEOUT_DEFAULT
    try:
        value = float(raw)
    except (TypeError, ValueError):
        print(
            f"[render-tools] {RENDER_MCP_CONNECT_TIMEOUT_ENV}={raw!r} is not a "
            f"number; using {RENDER_MCP_CONNECT_TIMEOUT_DEFAULT}",
            file=sys.stderr,
        )
        return RENDER_MCP_CONNECT_TIMEOUT_DEFAULT
    # A non-positive timeout would mean "wait forever" to some clients and
    # "fail immediately" to others; refuse to guess.
    if value <= 0:
        return RENDER_MCP_CONNECT_TIMEOUT_DEFAULT
    return value


def _render_entry() -> dict:
    return {
        "url": RENDER_MCP_URL,
        "headers": {"Authorization": RENDER_MCP_AUTH},
        "connect_timeout": _mcp_connect_timeout(),
    }


def ensure_render_mcp(config: dict) -> bool:
    """Insert mcp_servers.render if missing. Returns True if changed."""
    mcp_servers = config.get("mcp_servers")
    if mcp_servers is None:
        config["mcp_servers"] = {"render": _render_entry()}
        return True
    if not isinstance(mcp_servers, dict):
        print(
            "[render-tools] mcp_servers is not a mapping; skipping render entry",
            file=sys.stderr,
        )
        return False
    if "render" in mcp_servers:
        return False
    mcp_servers["render"] = _render_entry()
    return True


def _bynara_entry() -> dict:
    return {
        "name": "bynara",
        "base_url": BYNARA_BASE_URL,
        "key_env": BYNARA_API_KEY_ENV,
    }


def ensure_bynara_provider(config: dict) -> bool:
    """Insert the Bynara custom provider if the user has not defined it."""
    providers = config.get("custom_providers")
    if providers is None:
        config["custom_providers"] = [_bynara_entry()]
        return True
    if not isinstance(providers, list):
        print(
            "[render-tools] custom_providers is not a list; skipping Bynara",
            file=sys.stderr,
        )
        return False
    for provider in providers:
        if isinstance(provider, dict) and provider.get("name") == "bynara":
            return False
    providers.append(_bynara_entry())
    return True


def ensure_bynara_default(config: dict) -> bool:
    """Use Bynara on a fresh/default setup when its key is configured.

    Do not change an explicit provider or model. This makes the Blueprint
    work immediately with BYNARA_API_KEY while preserving later dashboard
    or restored-storage choices.
    """
    if not os.environ.get(BYNARA_API_KEY_ENV):
        return False
    model = config.get("model")
    if model is None:
        config["model"] = {
            "default": BYNARA_DEFAULT_MODEL,
            "provider": "custom:bynara",
        }
        return True
    if not isinstance(model, dict):
        return False
    current_provider = model.get("provider")
    current_default = model.get("default", model.get("model"))
    if current_provider not in (None, "auto"):
        return False
    if current_default not in (None, "anthropic/claude-opus-4.6"):
        return False
    model["default"] = BYNARA_DEFAULT_MODEL
    model["provider"] = "custom:bynara"
    return True


def _ensure_mapping(parent: dict, key: str) -> dict | None:
    value = parent.get(key)
    if value is None:
        value = {}
        parent[key] = value
    if not isinstance(value, dict):
        print(
            f"[render-tools] {key} is not a mapping; skipping its Free defaults",
            file=sys.stderr,
        )
        return None
    return value


def _set_free_default(
    mapping: dict,
    key: str,
    value: object,
    *,
    fresh: bool,
    upstream_values: tuple[object, ...] = (),
) -> bool:
    """Set a Free default, replacing only known template defaults on first boot."""
    if key not in mapping:
        mapping[key] = value
        return True
    if fresh and mapping[key] in upstream_values and mapping[key] != value:
        mapping[key] = value
        return True
    return False


def ensure_free_resource_defaults(config: dict, *, fresh: bool = False) -> list[str]:
    """Add settings supported by the pinned Hermes release for Free.

    On the first boot only, values matching the pinned upstream template's
    known defaults are lowered. A restored or dashboard-edited config remains
    authoritative on later boots.
    """
    changed: list[str] = []

    agent = _ensure_mapping(config, "agent")
    if agent is not None:
        for key, value, upstream_values in (
            ("max_turns", 30, (60, 90)),
            ("api_max_retries", 1, (3,)),
            ("gateway_timeout", 900, (1800,)),
        ):
            if _set_free_default(
                agent, key, value, fresh=fresh, upstream_values=upstream_values
            ):
                changed.append(f"agent.{key} = {value}")

    delegation = _ensure_mapping(config, "delegation")
    if delegation is not None:
        for key, value, upstream_values in (
            ("max_iterations", 20, (50,)),
            ("max_concurrent_children", 1, (3,)),
            ("max_spawn_depth", 1, (1,)),
            ("orchestrator_enabled", False, (True,)),
        ):
            if _set_free_default(
                delegation, key, value, fresh=fresh, upstream_values=upstream_values
            ):
                changed.append(f"delegation.{key} = {value}")

    auxiliary = _ensure_mapping(config, "auxiliary")
    if auxiliary is not None:
        session_search = _ensure_mapping(auxiliary, "session_search")
        if session_search is not None and _set_free_default(
            session_search,
            "max_concurrency",
            1,
            fresh=fresh,
            upstream_values=(3,),
        ):
            changed.append("auxiliary.session_search.max_concurrency = 1")

    compression = _ensure_mapping(config, "compression")
    if compression is not None:
        for key, value, upstream_values in (
            ("threshold", 0.40, (0.50,)),
            ("target_ratio", 0.15, (0.20,)),
            ("protect_last_n", 12, (20,)),
            ("hygiene_hard_message_limit", 250, (400,)),
        ):
            if _set_free_default(
                compression, key, value, fresh=fresh, upstream_values=upstream_values
            ):
                changed.append(f"compression.{key} = {value}")

    goals = _ensure_mapping(config, "goals")
    if goals is not None and _set_free_default(
        goals, "max_turns", 8, fresh=fresh, upstream_values=(20,)
    ):
        changed.append("goals.max_turns = 8")

    code_execution = _ensure_mapping(config, "code_execution")
    if code_execution is not None:
        for key, value, upstream_values in (
            ("timeout", 180, (300,)),
            ("max_tool_calls", 30, (50,)),
        ):
            if _set_free_default(
                code_execution, key, value, fresh=fresh, upstream_values=upstream_values
            ):
                changed.append(f"code_execution.{key} = {value}")

    browser = _ensure_mapping(config, "browser")
    if browser is not None:
        for key, value, upstream_values in (
            ("inactivity_timeout", 60, (120,)),
            ("command_timeout", 20, (30,)),
        ):
            if _set_free_default(
                browser, key, value, fresh=fresh, upstream_values=upstream_values
            ):
                changed.append(f"browser.{key} = {value}")

    file_read_max_chars = _set_free_default(
        config, "file_read_max_chars", 50_000, fresh=fresh, upstream_values=(100_000,)
    )
    if file_read_max_chars:
        changed.append("file_read_max_chars = 50000")

    tool_output = _ensure_mapping(config, "tool_output")
    if tool_output is not None:
        for key, value, upstream_values in (
            ("max_bytes", 30_000, (50_000,)),
            ("max_lines", 1_000, (2_000,)),
            ("max_line_length", 1_500, (2_000,)),
        ):
            if _set_free_default(
                tool_output, key, value, fresh=fresh, upstream_values=upstream_values
            ):
                changed.append(f"tool_output.{key} = {value}")

    return changed


def dedupe_enabled_toolsets(config: dict) -> list[str]:
    """Drop toolsets the pinned runtime rejects as duplicates.

    At v2026.5.7, a config that enables both ``web`` and ``web-search`` makes
    the tool registry reject ``web`` with
    "Tool registration REJECTED: 'web_search' (toolset 'web') would shadow
    existing tool from toolset 'web-search'" -- noisy, order-dependent, and
    confusing in the logs. The registry keeps ``web-search``'s tool either
    way, so removing ``web`` from the enabled list changes nothing at
    runtime. Opt out with HERMES_DEDUPE_TOOLSETS=0.
    """
    if os.environ.get("HERMES_DEDUPE_TOOLSETS") == "0":
        return []
    tools = config.get("tools")
    if not isinstance(tools, dict):
        return []
    enabled = tools.get("enabled_toolsets")
    if not isinstance(enabled, list):
        return []
    if "web-search" in enabled and "web" in enabled:
        tools["enabled_toolsets"] = [t for t in enabled if t != "web"]
        return ["tools.enabled_toolsets -= web (shadowed by web-search)"]
    return []


def ensure_external_skill_dirs(config: dict) -> list[str]:
    """Append the render-tools skill dirs to skills.external_dirs if missing.

    Returns the list of paths that were actually added.
    """
    skills = config.setdefault("skills", {})
    if not isinstance(skills, dict):
        print(
            "[render-tools] skills is not a mapping; skipping external_dirs",
            file=sys.stderr,
        )
        return []
    existing = skills.get("external_dirs")
    if existing is None:
        skills["external_dirs"] = list(RENDER_SKILL_DIRS)
        return list(RENDER_SKILL_DIRS)
    if not isinstance(existing, list):
        print(
            "[render-tools] skills.external_dirs is not a list; skipping",
            file=sys.stderr,
        )
        return []
    added: list[str] = []
    for path in RENDER_SKILL_DIRS:
        if path not in existing:
            existing.append(path)
            added.append(path)
    return added


def save_config(path: Path, config: dict) -> None:
    text = yaml.safe_dump(
        config,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )
    tmp = path.with_suffix(path.suffix + ".render-tools.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: patch-config.py <path/to/config.yaml>", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    path.parent.mkdir(parents=True, exist_ok=True)
    config = load_config(path)
    changed_mcp = ensure_render_mcp(config)
    changed_bynara = ensure_bynara_provider(config)
    changed_bynara_default = ensure_bynara_default(config)
    apply_free_defaults = os.environ.get("RENDER_TOOLS_APPLY_FREE_DEFAULTS") == "1"
    resource_defaults = ensure_free_resource_defaults(
        config, fresh=apply_free_defaults
    )
    added_dirs = ensure_external_skill_dirs(config)
    deduped = dedupe_enabled_toolsets(config)
    if changed_mcp or changed_bynara or changed_bynara_default or resource_defaults or added_dirs or deduped:
        save_config(path, config)
        parts = []
        if changed_mcp:
            parts.append("mcp_servers.render")
        if changed_bynara:
            parts.append("custom_providers += bynara")
        if changed_bynara_default:
            parts.append(f"model.default = {BYNARA_DEFAULT_MODEL}")
        parts.extend(resource_defaults)
        for dir_path in added_dirs:
            parts.append(f"skills.external_dirs += {dir_path}")
        parts.extend(deduped)
        print(f"[render-tools] patched {path}: {', '.join(parts)}")
    else:
        print(f"[render-tools] {path} already has render MCP + skill dirs; nothing to do")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
