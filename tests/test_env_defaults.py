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
        "HERMES_TUI_MAX_SESSIONS",
        "MALLOC_TRIM_THRESHOLD_",
        "HERMES_MEMGUARD_WARN",
        "HERMES_MEMGUARD_PAUSE_SYNC",
        "HERMES_MEMGUARD_RESUME_SYNC",
        "HERMES_MEMGUARD_CRITICAL",
        "HERMES_MEMGUARD_DASHBOARD_PCT",
        "HERMES_KEEP_ALIVE_SECONDS",
        "HERMES_KEEP_ALIVE_PATH",
        "GIT_STATE_WATCH_SECONDS",
        "GIT_STATE_DEBOUNCE_SECONDS",
        "GIT_STATE_MIN_PUSH_INTERVAL_SECONDS",
        "GIT_STATE_INTERVAL_SECONDS",
        "GIT_STATE_PACK_THREADS",
        "GIT_STATE_PACK_WINDOW_MEMORY_MB",
        "GIT_STATE_HTTP_POST_BUFFER_MB",
        "GIT_STATE_BIG_FILE_THRESHOLD_KB",
        "GIT_STATE_MAX_MEMORY_PCT",
    ):
        assert example.get(key) == common.get(key), f"{key} drifted between .env.example and env/common.env"
