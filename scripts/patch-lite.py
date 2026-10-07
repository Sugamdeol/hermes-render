"""Small, exact patches for the pinned Hermes release. Fail closed on upgrades."""
from pathlib import Path
import sys

root = Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes")
path = root / 'run_agent.py'
text = path.read_text()
old = '        self._api_max_retries = _api_retries\n'
new = '        self._api_max_retries = min(_api_retries, max(1, int(os.environ.get("HERMES_API_MAX_RETRIES", "2"))))\n'
assert old in text, 'agent retry cap patch no longer matches pinned source'
path.write_text(text.replace(old, new, 1))
path = root / "gateway/run.py"
old = "_AGENT_CACHE_MAX_SIZE = 128\n_AGENT_CACHE_IDLE_TTL_SECS = 3600.0  # evict agents idle for >1h\n"
new = '''_AGENT_CACHE_MAX_SIZE = max(1, int(os.environ.get("HERMES_AGENT_CACHE_MAX_SIZE", "1")))
_AGENT_CACHE_IDLE_TTL_SECS = max(1.0, float(os.environ.get("HERMES_AGENT_CACHE_IDLE_TTL_SECONDS", "30")))
'''
text = path.read_text()
assert old in text, "gateway cache patch no longer matches pinned source"
path.write_text(text.replace(old, new, 1))

# JSON-RPC clients must also run their agent outside the dashboard process.
# Keep the native protocol by forwarding to Hermes' existing stdio backend.
path = root / "hermes_cli/web_server.py"
import runpy
bridge_patch = runpy.run_path(str(Path(__file__).with_name("patch-chat-bridge.py")))
path.write_text(bridge_patch["patch"](path.read_text()))
dashboard_patch = runpy.run_path(str(Path(__file__).with_name("patch-dashboard.py")))
path.write_text(dashboard_patch["patch"](path.read_text()))

path = root / "hermes_cli/web_server.py"
old = '@app.websocket("/api/pty")\nasync def pty_ws(ws: WebSocket) -> None:\n'
new = '''@app.websocket("/api/pty")
async def pty_ws(ws: WebSocket) -> None:
    if _lite_pty_lock.locked():
        await ws.close(code=4429)
        return
    async with _lite_pty_lock:
        await _lite_pty_ws(ws)


async def _lite_pty_ws(ws: WebSocket) -> None:
'''
text = path.read_text()
assert old in text, "native chat patch no longer matches pinned source"
path.write_text(text.replace(old, new, 1))

# A shorter TTL is ineffective when the native watcher wakes only every 5 min.
path = root / "gateway/run.py"
text = path.read_text()
old = "async def _session_expiry_watcher(self, interval: int = 300):"
assert old in text, "cache sweep patch no longer matches pinned source"
path.write_text(text.replace(old, "async def _session_expiry_watcher(self, interval: int = 30):", 1))

# The PTY leader spawns Node and a Python backend. Killing only that leader
# can leave its descendants consuming RAM after every browser reconnect.
path = root / "hermes_cli/pty_bridge.py"
text = path.read_text()
old = """            if not self._proc.isalive():
                break
            try:
                self._proc.kill(sig)
            except Exception:
                pass
"""
new = """            try:
                # PtyProcess.spawn creates a new session/process group.
                # Signal it even when the leader has already exited.
                os.killpg(self.pid, sig)
            except ProcessLookupError:
                break
            except PermissionError:
                pass
"""
assert old in text, "PTY cleanup patch no longer matches pinned source"
path.write_text(text.replace(old, new, 1))

# All native browser agents (including descendants) enter the shared budget.
path = root / "hermes_cli/pty_bridge.py"
text = path.read_text()
old = "            list(argv),\n            cwd=cwd,"
new = "            [sys.executable, '/opt/render-tools/agent-budget.py', 'run', 'chat', *argv],\n            cwd=cwd,"
assert old in text, "PTY budget wrapper patch no longer matches pinned source"
path.write_text(text.replace(old, new, 1))

path = root / "hermes_cli/web_server.py"
text = path.read_text()
old = '    cmd = [sys.executable, "-m", "hermes_cli.main", *subcommand]'
new = old + "\n    if name == 'gateway-restart':\n        cmd = [sys.executable, '/opt/render-tools/agent-budget.py', 'run', 'gateway', *cmd]"
assert old in text, "gateway action budget patch no longer matches pinned source"
text = text.replace(old, new, 1)
old = "            if chunk is None:  # EOF\n                return"
new = "            if chunk is None:  # EOF\n                try:\n                    await ws.close(code=1013)\n                except Exception:\n                    pass\n                return"
assert old in text, "PTY EOF patch no longer matches pinned source"
path.write_text(text.replace(old, new, 1))

# Apply allocation limits inside Python agent backends, never the Node launcher.
(root / "hermes_cli/render_memory.py").write_text(Path(__file__).with_name("worker-memory.py").read_text())
path = root / "gateway/run.py"
text = path.read_text()
old = "    def __init__(self, config: Optional[GatewayConfig] = None):\n"
new = old + "        from hermes_cli.render_recovery import session_concurrency\n        self._render_agent_semaphore = asyncio.Semaphore(session_concurrency())\n        self._render_checkpoint_lock = asyncio.Lock()\n        from hermes_cli.render_memory import apply_worker_limit\n        apply_worker_limit()\n"
assert old in text, "gateway allocation limit patch no longer matches pinned source"
text = text.replace(old, new, 1)
path.write_text(text)

# Bound cross-chat concurrency with a configurable number of slots. Lanes keep
# their own session state while waiting; the semaphore is held for the full
# agent turn, including its tool loop and pending-message drains.
old = """            # Run the agent
            agent_result = await self._run_agent(
                message=message_text,
                context_prompt=context_prompt,
                history=history,
                source=source,
                session_id=session_entry.session_id,
                session_key=session_key,
                run_generation=run_generation,
                event_message_id=event.message_id,
                channel_prompt=event.channel_prompt,
            )
"""
new = old.replace("await self._run_agent(", "await self._render_serialized_agent(", 1)
assert old in text, "gateway turn serialization patch no longer matches pinned source"
text = text.replace(old, new, 1)

# Keep recursion inside an already-held turn; only the initial turn entry
# acquires the semaphore, so native pending-message follow-ups cannot deadlock.
helper = """

async def _render_serialized_agent(self, *args, **kwargs):
    async with self._render_agent_semaphore:
        from hermes_cli import render_recovery
        async with self._render_checkpoint_lock:
            await render_recovery.checkpoint()
        if render_recovery.is_terminal(kwargs.get('session_key')):
            return {"interrupted": True, "completed": False, "final_response": ""}
        return await self._run_agent(*args, **kwargs)


GatewayRunner._render_serialized_agent = _render_serialized_agent
"""
entrypoint = 'if __name__ == "__main__":\n    main()'
assert entrypoint in text, "gateway CLI entrypoint patch no longer matches pinned source"
text = text.replace(entrypoint, helper + "\n" + entrypoint, 1)
path.write_text(text)
path = root / "tui_gateway/entry.py"
text = path.read_text()
old = "from tui_gateway import server\n"
new = "from hermes_cli.render_memory import apply_worker_limit\napply_worker_limit()\n\n" + old
assert old in text, "TUI allocation limit patch no longer matches pinned source"
path.write_text(text.replace(old, new, 1))

# Avoid importing a second complete HermesCLI for ordinary browser messages.
path = root / "tui_gateway/server.py"
text = path.read_text()
old = "            try:\n                worker = _SlashWorker(key, getattr(agent, \"model\", _resolve_model()))\n                current[\"slash_worker\"] = worker\n            except Exception:\n                pass\n"
new = "            # Slash subprocess starts on the first slash command.\n"
assert old in text, 'lazy slash worker patch no longer matches pinned source'
text = text.replace(old, new, 1)
old = "    try:\n        _sessions[sid][\"slash_worker\"] = _SlashWorker(\n            key, getattr(agent, \"model\", _resolve_model())\n        )\n    except Exception:\n        # Defer hard-failure to slash.exec; chat still works without slash worker.\n        _sessions[sid][\"slash_worker\"] = None\n"
new = "    # slash.exec already creates this lazily when actually needed.\n"
assert old in text, 'lazy slash worker patch no longer matches pinned source'
text = text.replace(old, new, 1)
old = "def _restart_slash_worker(session: dict):\n    worker = session.get(\"slash_worker\")\n"
new = "def _restart_slash_worker(session: dict):\n    worker = session.get(\"slash_worker\")\n    if worker is None:\n        return\n"
assert old in text, 'lazy slash worker patch no longer matches pinned source'
text = text.replace(old, new, 1)
path.write_text(text)

# Persist unfinished gateway intent before agent allocation and recover its exact lane.
(root / 'hermes_cli/render_recovery.py').write_text(Path(__file__).with_name('task-recovery.py').read_text())
path = root / 'gateway/run.py'
text = path.read_text()
old = "        if getattr(session_entry, \"was_auto_reset\", False):\n"
new = "        from hermes_cli import render_recovery\n        render_recovery.begin(session_entry, event)\n        if getattr(session_entry, \"was_auto_reset\", False):\n"
assert old in text, 'durable recovery patch no longer matches pinned source'
text = text.replace(old, new, 1)
old = "        # Stuck-loop detection (#7536): if a session has been active across\n"
new = "        from hermes_cli import render_recovery\n        render_recovery.recover(self.session_store)\n\n        # Stuck-loop detection (#7536): if a session has been active across\n"
assert old in text, 'durable recovery patch no longer matches pinned source'
text = text.replace(old, new, 1)
old = "        for session_key in stuck_keys:\n            try:\n"
new = "        for session_key in stuck_keys:\n            from hermes_cli import render_recovery\n            if render_recovery.tracked(session_key):\n                continue  # Pause retry exhaustion without wiping its session ID.\n            try:\n"
assert old in text, 'durable recovery patch no longer matches pinned source'
text = text.replace(old, new, 1)
old = "            if marker is not None and (now - marker).total_seconds() > window:\n"
new = "            from hermes_cli import render_recovery\n            if not render_recovery.tracked(entry.session_key) and marker is not None and (now - marker).total_seconds() > window:\n"
assert old in text, 'durable recovery patch no longer matches pinned source'
text = text.replace(old, new, 1)
old = "                text=\"\",\n                message_type=MessageType.TEXT,\n                source=source,\n                internal=True,\n"
new = "                text=render_recovery.resume_text(entry.session_key),\n                message_type=MessageType.TEXT,\n                source=source,\n                internal=True,\n"
assert old in text, 'durable recovery patch no longer matches pinned source'
text = text.replace(old, new, 1)
old = "                self._clear_restart_failure_count(session_key)\n                try:\n"
new = "                self._clear_restart_failure_count(session_key)\n                from hermes_cli import render_recovery\n                try:\n"
assert old in text, 'durable recovery patch no longer matches pinned source'
text = text.replace(old, new, 1)
old = "                and _interruption_is_fresh\n            )\n            _has_fresh_tool_tail"
new = "                and (_interruption_is_fresh or __import__(\"hermes_cli.render_recovery\", fromlist=[\"tracked\"]).tracked(session_key))\n            )\n            _has_fresh_tool_tail"
assert old in text, 'durable recovery patch no longer matches pinned source'
text = text.replace(old, new, 1)
old = "        for entry in candidates:\n            marker = entry.last_resume_marked_at or entry.updated_at\n"
new = "        from hermes_cli import render_recovery\n        candidates.sort(key=lambda entry: render_recovery.priority(entry.session_key))\n        queued = getattr(self, '_render_recovery_queued', set())\n        self._render_recovery_queued = queued\n        for entry in candidates:\n            if entry.session_key in queued:\n                continue\n            marker = entry.last_resume_marked_at or entry.updated_at\n"
assert old in text, 'multi-session recovery patch no longer matches pinned source'
text = text.replace(old, new, 1)
old = "            task = asyncio.create_task(adapter.handle_message(event))\n            self._background_tasks.add(task)\n            task.add_done_callback(self._background_tasks.discard)\n            scheduled += 1\n"
new = "            queued.add(entry.session_key)\n            task = asyncio.create_task(render_recovery.dispatch(self, adapter, entry, event, entry.session_id))\n            self._background_tasks.add(task)\n            task.add_done_callback(self._background_tasks.discard)\n            task.add_done_callback(lambda task, key=entry.session_key: queued.discard(key))\n            scheduled += 1\n"
assert old in text, 'multi-session recovery patch no longer matches pinned source'
text = text.replace(old, new, 1)
old = "        self._schedule_resume_pending_sessions()\n\n        # Drain any recovered process watchers"
new = "        self._schedule_resume_pending_sessions()\n        from hermes_cli import render_recovery\n        recovery_watch = asyncio.create_task(render_recovery.watch(self))\n        self._background_tasks.add(recovery_watch)\n        recovery_watch.add_done_callback(self._background_tasks.discard)\n\n        # Drain any recovered process watchers"
assert old in text, 'continuous recovery patch no longer matches pinned source'
text = text.replace(old, new, 1)
old = '                if bound_session_id and bound_session_id != session_entry.session_id:\n'
new = '                from hermes_cli import render_recovery\n                if bound_session_id != session_entry.session_id and render_recovery.pending_match(session_entry):\n                    self._record_telegram_topic_binding(source, session_entry)\n                    bound_session_id = session_entry.session_id\n                if bound_session_id and bound_session_id != session_entry.session_id:\n'
assert old in text, 'topic recovery identity patch no longer matches'
text = text.replace(old, new, 1)
# Compression rebinding and transcript commit must precede recovery completion.
old = '                session_entry.session_id = agent_result["session_id"]\n'
new = '                from hermes_cli import render_recovery\n                render_recovery.rebind(self.session_store, session_key, agent_result["session_id"], self._session_db)\n'
assert old in text, 'result recovery binding patch no longer matches'
text = text.replace(old, new, 1)
old = "                                    if _hyg_new_sid != session_entry.session_id:\n                                        session_entry.session_id = _hyg_new_sid\n                                        self.session_store._save()"
new = "                                    if _hyg_new_sid != session_entry.session_id:\n                                        from hermes_cli import render_recovery\n                                        render_recovery.rebind(self.session_store, session_key, _hyg_new_sid, self._session_db)"
assert old in text, 'hygiene recovery binding patch no longer matches'
text = text.replace(old, new, 1)
old = "                if entry:\n                    entry.session_id = agent.session_id\n                    self.session_store._save()"
new = "                if entry:\n                    from hermes_cli import render_recovery\n                    render_recovery.rebind(self.session_store, session_key, agent.session_id, self._session_db)"
assert old in text, 'compression recovery binding patch no longer matches'
text = text.replace(old, new, 1)
old = "            # Auto voice reply: send TTS audio before the text response"
new = "            from hermes_cli import render_recovery\n            if session_key and render_recovery.turn_finished(agent_result):\n                render_recovery.finish(session_key, self.session_store)\n\n" + old
assert old in text, 'recovery transcript commit patch no longer matches'
text = text.replace(old, new, 1)
# Stop/reset must persist intent cancellation even through the early intercept.
old = '        running_agent = self._running_agents.get(session_key)\n        if running_agent and running_agent is not _AGENT_PENDING_SENTINEL:\n'
new = '        if interrupt_reason in (_INTERRUPT_REASON_STOP, _INTERRUPT_REASON_RESET):\n            from hermes_cli import render_recovery\n            render_recovery.finish(session_key, self.session_store, status="stopped")\n' + old
assert old in text, 'stop recovery patch no longer matches'
text = text.replace(old, new, 1)
old = '        agent = self._running_agents.get(session_key)\n        if agent is _AGENT_PENDING_SENTINEL:\n'
# This occurrence is in the fallback /stop handler; scope the replacement there.
start = text.index('    async def _handle_stop_command(')
head, tail = text[:start], text[start:]
assert old in tail, 'stop fallback recovery patch no longer matches'
tail = tail.replace(old, '        from hermes_cli import render_recovery\n        render_recovery.finish(session_key, self.session_store, status="stopped")\n' + old, 1)
text = head + tail
path.write_text(text)

# An unfinished intent protects the lane even before the periodic scanner runs.
path = root / 'gateway/session.py'
text = path.read_text()
old = '                elif entry.resume_pending:\n'
new = '                elif entry.resume_pending or __import__("hermes_cli.render_recovery", fromlist=["pending_match"]).pending_match(entry):\n'
assert old in text, 'unfinished lane expiry patch no longer matches'
path.write_text(text.replace(old, new, 1))
