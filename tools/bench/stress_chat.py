#!/usr/bin/env python3
"""Drive the browser chat exactly the way the dashboard plugin does.

The in-browser Chat tab talks JSON-RPC over ``/api/ws`` to ``tui_gateway``:
``session.create`` → ``prompt.submit`` → ``message.delta``* → ``message.complete``.
That path is where a 512 MB instance dies, because every session keeps a full
``AIAgent`` (LLM clients, tool schemas, memory providers) inside the
*dashboard* process. This driver opens real WebSockets against a running
dashboard and reports what the container did with them:

  * idle / after / delta memory from the cgroup,
  * how much of that came back after the sockets closed,
  * whether the growth is a leak (monotonic) or a plateau.

Needs the ``websockets`` package (already a Hermes dashboard dependency) and a
dashboard with ``HERMES_DASHBOARD_TUI=1``. Point ``--model-backend`` at
tools/bench/mock_llm.py when there is no real API key.

Usage:
    stress_chat.py --base http://127.0.0.1:10000 --cgroup /sys/fs/cgroup/hb2 \
        --conversations 20 --turns 2 --settle 10
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import memwatch  # noqa: E402

try:
    import websockets
except ImportError:  # pragma: no cover
    print("needs the 'websockets' package", file=sys.stderr)
    raise SystemExit(2)

TOKEN_RE = re.compile(r'__HERMES_SESSION_TOKEN__="([^"]+)"')


def fetch_token(base: str) -> str:
    """Scrape the ephemeral dashboard token the SPA is handed at ``/``."""
    with urllib.request.urlopen(base.rstrip("/") + "/", timeout=20) as resp:
        html = resp.read().decode("utf-8", "replace")
    match = TOKEN_RE.search(html)
    if not match:
        raise SystemExit("could not find the session token in the dashboard HTML")
    return match.group(1)


class ChatClient:
    """One browser tab: a WebSocket plus the JSON-RPC ids it is waiting on."""

    def __init__(self, url: str, timeout: float = 90.0) -> None:
        self.url = url
        self.timeout = timeout
        self.ws = None
        self.next_id = 0
        self.session_id = ""
        self.events: list[str] = []
        self.last_text = ""
        self.deltas = 0

    async def connect(self) -> None:
        self.ws = await websockets.connect(
            self.url, max_size=8 * 1024 * 1024, open_timeout=self.timeout
        )
        await self._expect_event("gateway.ready")

    async def _recv(self) -> dict:
        raw = await asyncio.wait_for(self.ws.recv(), timeout=self.timeout)
        return json.loads(raw)

    async def _expect_event(self, name: str) -> dict:
        while True:
            msg = await self._recv()
            params = msg.get("params") or {}
            if msg.get("method") == "event" and params.get("type") == name:
                return msg

    async def call(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        rid = self.next_id
        await self.ws.send(json.dumps(
            {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
        ))
        while True:
            msg = await self._recv()
            if msg.get("method") == "event":
                params = msg.get("params") or {}
                etype = params.get("type") or ""
                self.events.append(etype)
                if etype == "message.delta":
                    self.deltas += 1
                elif etype == "message.complete":
                    payload = params.get("payload") or {}
                    self.last_text = str(payload.get("text") or "")[:200]
                continue
            if msg.get("id") == rid:
                if "error" in msg:
                    raise RuntimeError(f"{method} failed: {msg['error']}")
                return msg.get("result") or {}

    async def create_session(self) -> str:
        result = await self.call("session.create", {"cols": 100})
        self.session_id = str(result.get("session_id") or "")
        return self.session_id

    async def prompt(self, text: str) -> str:
        """Submit one turn and wait for message.complete."""
        self.deltas = 0
        deadline = time.time() + self.timeout
        await self.call("prompt.submit",
                        {"session_id": self.session_id, "text": text})
        while time.time() < deadline:
            msg = await self._recv()
            params = msg.get("params") or {}
            etype = params.get("type") or ""
            self.events.append(etype)
            if etype == "message.delta":
                self.deltas += 1
            elif etype == "message.complete":
                payload = params.get("payload") or {}
                return str(payload.get("text") or "")
            elif etype == "error":
                raise RuntimeError(f"agent error: {params.get('payload')}")
        raise TimeoutError("no message.complete before the deadline")

    async def close_session(self) -> None:
        if self.session_id:
            try:
                await self.call("session.close", {"session_id": self.session_id})
            except Exception:
                pass

    async def close(self) -> None:
        if self.ws is not None:
            await self.ws.close()
            self.ws = None


def mb(sample: dict) -> float:
    return sample["used_mb"] or sample["rss_sum_mb"]


async def run(args: argparse.Namespace) -> int:
    token = fetch_token(args.base)
    ws_url = (args.base.replace("http://", "ws://").replace("https://", "wss://")
              .rstrip("/") + f"/api/ws?token={token}")
    cg = args.cgroup or None

    def snap(label: str) -> dict:
        s = memwatch.sample(cg)
        print(f"[{label}] {memwatch.format_line(s)}", flush=True)
        return s

    if args.settle_before:
        await asyncio.sleep(args.settle_before)
    idle = snap("idle")

    prompt_text = args.prompt
    completed = 0
    failures: list[str] = []
    series: list[tuple[str, float]] = [("idle", mb(idle))]

    # ── phase 1: sequential conversations, each on its own tab ────────────
    # This is the shape that used to leak: a new tab per conversation, closed
    # afterwards, with the in-process agent expected to go away with it.
    for index in range(args.conversations):
        client = ChatClient(ws_url, timeout=args.timeout)
        try:
            await client.connect()
            await client.create_session()
            for turn in range(args.turns):
                await client.prompt(f"{prompt_text} (conversation {index} turn {turn})")
                completed += 1
            if not args.keep_open:
                await client.close()
        except Exception as exc:
            failures.append(f"conversation {index}: {type(exc).__name__}: {exc}")
            print(f"  ! {failures[-1]}", flush=True)
        finally:
            if args.keep_open:
                # deliberately leaked: models "user leaves 20 tabs open"
                pass
            else:
                try:
                    await client.close()
                except Exception:
                    pass
        if (index + 1) % max(1, args.report_every) == 0:
            s = snap(f"after {index + 1} conversations")
            series.append((f"conv{index + 1}", mb(s)))

    if args.keep_open:
        after_open = snap("all tabs still open")
        series.append(("tabs-open", mb(after_open)))

    # ── phase 2: reconnect churn on one session ───────────────────────────
    for index in range(args.reconnects):
        client = ChatClient(ws_url, timeout=args.timeout)
        try:
            await client.connect()
            await client.create_session()
            await client.close()
        except Exception as exc:
            failures.append(f"reconnect {index}: {type(exc).__name__}: {exc}")
    if args.reconnects:
        s = snap(f"after {args.reconnects} connect/disconnect cycles")
        series.append(("reconnects", mb(s)))

    # ── settle: what actually comes back once nothing is attached ─────────
    await asyncio.sleep(args.settle)
    final = snap("settled")
    series.append(("settled", mb(final)))

    print("\n── memory trace ──", flush=True)
    base = mb(idle)
    for label, value in series:
        print(f"  {label:>12}: {value:7.1f} MB  ({value - base:+.1f})", flush=True)
    peak = max(value for _, value in series)
    print(
        f"\nRESULT: idle={base:.1f}MB peak={peak:.1f}MB settled={mb(final):.1f}MB "
        f"residual={mb(final) - base:+.1f}MB "
        f"cgroup_peak={final['peak_mb']:.1f}MB "
        f"turns={completed} failures={len(failures)}",
        flush=True,
    )
    if failures:
        print("FAILURES:", flush=True)
        for line in failures[:10]:
            print(f"  - {line}", flush=True)
    limit = final["limit_mb"]
    if limit and peak > limit * 0.9:
        print(f"VERDICT: peak {peak:.0f}MB is within 10% of the {limit:.0f}MB cap",
              flush=True)
        return 1
    return 0 if not failures else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="http://127.0.0.1:10000")
    ap.add_argument("--cgroup", default="")
    ap.add_argument("--conversations", type=int, default=10)
    ap.add_argument("--turns", type=int, default=1)
    ap.add_argument("--reconnects", type=int, default=0)
    ap.add_argument("--keep-open", action="store_true",
                    help="never close the WebSockets (models abandoned tabs)")
    ap.add_argument("--prompt", default="Say hello in one short sentence.")
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--settle", type=float, default=10.0)
    ap.add_argument("--settle-before", type=float, default=0.0)
    ap.add_argument("--report-every", type=int, default=5)
    args = ap.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
