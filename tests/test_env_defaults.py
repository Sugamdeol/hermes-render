from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def test_env_example_matches_boot_defaults_for_chat_and_free_tier_guards():
    common = _parse_env_file(ROOT / "env" / "common.env")
    example = _parse_env_file(ROOT / ".env.example")

    for key in (
        "HERMES_DASHBOARD",
        "HERMES_DASHBOARD_HOST",
        "HERMES_DASHBOARD_PORT",
        "HERMES_DASHBOARD_TUI",
        "HERMES_TUI_DISABLE_SLASH_WORKER",
        "HERMES_TUI_RPC_POOL_WORKERS",
        "HERMES_TUI_CLOSE_SESSIONS_ON_DISCONNECT",
        "NODE_OPTIONS",
        "MALLOC_ARENA_MAX",
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "TOKENIZERS_PARALLELISM",
        "MAKEFLAGS",
        "HERMES_AGENT_CACHE_MAX_SIZE",
        "HERMES_AGENT_CACHE_IDLE_TTL_SECONDS",
    ):
        assert example.get(key) == common.get(key), f"{key} drifted between .env.example and env/common.env"
