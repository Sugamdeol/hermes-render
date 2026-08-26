#!/opt/hermes/.venv/bin/python
"""Pre-flight check for TELEGRAM_BOT_TOKEN before the gateway starts.

A rejected Telegram token is the fastest way this deployment ends up in a
Render crash loop. The gateway is the container's foreground process, and
when its only messaging platform cannot authenticate, `gateway run` exits
1 — killing the background dashboard with it. Render restarts the
container, which fails the same way ~90 seconds later, producing the
"Instance failed: Exited with status 1" / "Service recovered" flapping
while the dashboard is never up long enough to diagnose anything.

Before handing off to the gateway, this script asks Telegram's getMe
endpoint to render a verdict on the configured token:

  - HTTP 2xx             -> token works. Exit 0.
  - HTTP 401/404         -> Telegram authoritatively rejected the token
                            (revoked via BotFather, bot deleted, or a
                            mistyped value). Print remediation steps and
                            exit 3 so bootstrap.sh can start the gateway
                            WITHOUT Telegram instead of crash-looping.
                            The dashboard, cron scheduler, and GoFile
                            state sync keep running; updating the env var
                            in Render restarts the service and re-enables
                            Telegram on the next boot.
  - 429 / 5xx / no route -> transient. Exit 0 and leave the token alone;
                            the gateway's own connect/reconnect retries
                            handle transient failures far better than a
                            one-shot boot check could.

Whitespace-only damage is repaired rather than rejected: when the raw
value has surrounding whitespace but its trimmed form passes getMe, the
trimmed token is printed on stdout and exit code 4 asks bootstrap.sh to
re-export it (pasting a token with a trailing newline into dashboard
forms is common).

Exit codes (the contract with bootstrap.sh):
    0  continue normally (token OK, or verdict unknowable/transient)
    3  Telegram definitively rejected or the value is not a real token;
       drop the platform for this boot
    4  token verified after trimming; stdout carries the cleaned value

All diagnostics go to stderr; stdout is used ONLY for the cleaned token
on exit 4, because bootstrap.sh captures stdout. The secret half of the
token is never written to either stream.

Standard library only, so it runs under the same interpreter as the
other render-tools scripts without new dependencies.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request

GETME_TIMEOUT_SECS = 8.0

# Bot API tokens have been `<bot_id>:<secret>` for years, with the secret
# restricted to [A-Za-z0-9_-]. python-telegram-bot enforces essentially the
# same shape locally and raises InvalidToken on anything else — which the
# gateway classifies as a (retryable) connect failure, i.e. the same crash
# loop. Rejecting hopeless values here keeps the service alive instead.
TOKEN_RE = re.compile(r"^\d{5,}:[\w-]{20,}$")

# Values that are obviously not real tokens (copied from docs, READMEs, or
# log output) are rejected without spending a network round trip.
PLACEHOLDER_VALUES = frozenset(
    {
        "changeme",
        "paste-token-here",
        "test",
        "token",
        "xxx",
        "your-bot-token",
        "your-token",
        "your_token",
        "yourtokhere",
    }
)

EXIT_OK = 0
EXIT_REJECTED = 3
EXIT_TRIMMED = 4


def warn(message: str) -> None:
    print(message, file=sys.stderr)


def describe_token(token: str) -> str:
    """A log-safe label for the configured token (never the secret half)."""
    head, sep, _ = token.partition(":")
    if sep:
        return f"bot id {head}"
    return f"a value with no ':' separator ({len(token)} chars)"


def token_problem(token: str) -> str | None:
    """Return a human explanation for a value that cannot be a real token."""
    if not token:
        return "token is empty"
    if "***" in token:
        return (
            "token contains '***' — this looks like a masked token copied from "
            "log output instead of the real value from @BotFather"
        )
    if token.lower() in PLACEHOLDER_VALUES:
        return "token is a placeholder value, not a real bot token"
    if not TOKEN_RE.match(token):
        return (
            "token is not in the `<bot_id>:<secret>` format Telegram issues "
            "via @BotFather"
        )
    return None


def getme(token: str) -> tuple[int | None, str | None]:
    """Call getMe once. Returns (http_status, description) — (None, cause)
    when the request never got an HTTP answer.

    The token travels in the URL path, so error text is built strictly from
    the status code and Telegram's JSON `description` field; urllib's raw
    exception strings are never surfaced verbatim.
    """
    url = f"https://api.telegram.org/bot{token}/getMe"
    request = urllib.request.Request(
        url,
        method="GET",
        headers={"User-Agent": "hermes-render-bootcheck/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=GETME_TIMEOUT_SECS) as resp:
            return resp.status, None
    except urllib.error.HTTPError as exc:
        description = None
        try:
            payload = json.loads(exc.read().decode("utf-8", "replace") or "{}")
            if isinstance(payload, dict):
                description = payload.get("description")
        except Exception:
            description = None
        return exc.code, description if isinstance(description, str) else None
    except Exception as exc:  # any transport failure is transient
        reason = getattr(exc, "reason", None)
        cause = f"{type(exc).__name__}"
        if reason is not None:
            cause += f": {type(reason).__name__}"
        return None, cause


def classify_http(status: int | None) -> str:
    """Map an HTTP status from getMe to a verdict.

    Only 401 and 404 count as Telegram authoritatively rejecting the
    token; those are what python-telegram-bot surfaces as InvalidToken.
    Everything else (rate limit, server error, no route) is transient.
    """
    if status is not None and 200 <= status < 300:
        return "ok"
    if status in (401, 404):
        return "rejected"
    return "transient"


def print_remediation(label: str, status: int | None, description: str | None) -> None:
    if status is None:
        detail = description or "no HTTP answer"
    else:
        detail = f"HTTP {status}"
        if description:
            detail += f" ({description})"
    warn(
        f"[render-tools] Telegram rejected TELEGRAM_BOT_TOKEN ({label}): {detail}.\n"
        "The token has been revoked (e.g. regenerated in BotFather), belongs to a\n"
        "deleted bot, or was mistyped. The gateway would exit(1) on this at startup,\n"
        "taking the whole container — including the dashboard — down with it.\n"
        "\n"
        "Fix it with these steps:\n"
        "  1. Message @BotFather on Telegram -> /mybots -> select your bot ->\n"
        "     API Token -> copy the CURRENT token (or /token to regenerate).\n"
        "  2. Render dashboard -> your service -> Environment -> edit\n"
        "     TELEGRAM_BOT_TOKEN and paste the new value.\n"
        "  3. Saving the env var restarts the service automatically; Telegram\n"
        "     reconnects on the next boot.\n"
        "\n"
        "For THIS boot the token is being ignored: the gateway starts without\n"
        "Telegram so the dashboard, cron scheduler, and state sync stay up."
    )


def main() -> int:
    raw = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    token = raw.strip()
    if not token:
        # No env token means the platform is not force-enabled; the gateway
        # handles the "no messaging platforms" case on its own.
        return EXIT_OK

    label = describe_token(token)

    problem = token_problem(token)
    if problem is not None:
        warn(f"[render-tools] TELEGRAM_BOT_TOKEN is misconfigured: {problem}.")
        print_remediation(label, None, "value failed the format pre-check")
        return EXIT_REJECTED

    status, description = getme(token)
    verdict = classify_http(status)
    if verdict == "rejected":
        print_remediation(label, status, description)
        return EXIT_REJECTED
    if verdict == "transient":
        cause = f"HTTP {status}" if status is not None else "no HTTP answer"
        warn(
            "[render-tools] telegram token pre-flight inconclusive "
            f"({cause}, {description}); leaving TELEGRAM_BOT_TOKEN set for "
            "the gateway's own connect retries"
        )
        return EXIT_OK

    if raw != token:
        # The trimmed value works. bootstrap.sh re-exports it from stdout.
        warn(
            "[render-tools] TELEGRAM_BOT_TOKEN had surrounding whitespace; "
            f"verified and trimmed (bot id {token.partition(':')[0]})"
        )
        print(token)
        return EXIT_TRIMMED

    warn(
        "[render-tools] telegram token pre-flight ok "
        f"(bot id {token.partition(':')[0]})"
    )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
