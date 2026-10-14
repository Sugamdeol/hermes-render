# Extracted from NousResearch/hermes-agent@0e427d788d253630d38a3a51cd76849e73cd6925.
# Real native admission and startup dispatch, with dependencies stubbed by tests.
from __future__ import annotations
import asyncio
import time

class NativeIngress:
    async def _handle_message(self, event: MessageEvent) -> Optional[str]:
        """Handle an incoming message from any platform: auth → command check → running-agent
        interrupt → get/create session → build context → run agent → return response."""
        from gateway.run import _AGENT_PENDING_SENTINEL
        _admitted = await self._hm_admit_event(event)
        if _admitted is None:
            return None
        event, source, is_internal = _admitted
        if not is_internal:
            from hermes_cli.observability.shared_metrics_events import record_gateway_slash_command
            record_gateway_slash_command(event)
        # TERMINAL-DECLINE LATCH TEARDOWN. Deliberately placed AFTER admission,
        # not on the adapter's raw inbound: profile routing, the ignored-channel
        # guard, plugin hooks and user authorization all reject events above,
        # and a rejected event must not be able to clear a refusal belonging to
        # an active turn. This is also the single entry point every lane shares
        # — Discord interaction passthrough builds its own MessageEvent and
        # calls handle_message directly, so a teardown on the relay's inbound
        # handler left those turns muted.

        _paused_notice = self._hm_estop_gate(event, source, is_internal)
        if _paused_notice is not None:
            return _paused_notice

        _quick_key = self._session_key_for_source(source)
        _reply = await self._hm_pending_reply_intercepts(event, source, _quick_key)
        if _reply is not None:
            return _reply

        # Evict a leaked/reaped ``_running_agents`` slot before the busy-session fast-path.
        self._hm_evict_idle_stale_agent(_quick_key)
        if self._is_session_running(_quick_key):
            self._hm_evict_reaped_agent(_quick_key)
        if self._is_session_running(_quick_key):
            return await self._hm_handle_running_session_message(event, source, _quick_key)

        _handled, _result = await self._hm_dispatch_idle_commands(event, source, _quick_key)
        if _handled:
            return _result

        # Pending exec approvals go through /approve and /deny only — no bare-text matching, or a
        # conversational "yes" would execute a dangerous command.
        if not is_internal:
            if await asyncio.to_thread(self._is_telegram_topic_root_lobby, source):
                # Debounced so a user who forgets about topic mode doesn't get ten reminders.
                if self._should_send_telegram_lobby_reminder(source):
                    return self._telegram_topic_root_lobby_message()
                return None
            # External-drain new-turn gate: when NAS engaged an external drain (.drain_request.json,
            # seen by _drain_control_watcher), refuse to START new turns so the in-flight set can
            # only fall to zero. Reversible.
            if self._external_drain_active:
                logger.info("Refusing new turn for session %s — external drain active.", _quick_key)
                return t("gateway.busy.draining_maintenance")

        # Claim this session before any await: many awaits sit between here and _run_agent
        # registering the real AIAgent; without this sentinel a second message during any of them
        # passes the "already running" guard and spins up a duplicate agent for the same session.
        _active_session_lease, _limit_message = self._claim_active_session_slot(_quick_key, source)
        if _limit_message is not None:
            logger.info("Rejecting new active session %s: max_concurrent_sessions reached", _quick_key)
            return _limit_message

        event, source, is_internal = self._hm_rescue_orphaned_fifo(event, source, is_internal, _quick_key)

        _claim_state = self._session_state(_quick_key)
        if _active_session_lease is not None:
            _claim_state.turn.lease = _active_session_lease
        _claim_state.turn.agent = _AGENT_PENDING_SENTINEL
        _claim_state.turn.event = event
        _claim_state.turn.started_ts = time.time()
        from hermes_cli.observability.shared_metrics_gateway import start_reply_clock
        start_reply_clock(source, internal=is_internal)
        self._persist_active_agents()
        _run_generation = self._begin_session_run_generation(_quick_key)

        try:
            if not is_internal:  # fail-open plugin consume, inside the claimed slot (#129958)
                from gateway.run_inbound_consumer import run_post_admission_hook
                _consumed, _consumer_reply = await run_post_admission_hook(self, event, source, _quick_key)
                if _consumed:
                    return _consumer_reply
            try:
                _agent_result = await self._handle_message_with_agent(event, source, _quick_key, _run_generation)
            except TurnLeaseTimeoutError as exc:
                # A rejected message, not a completed turn: return before the /goal judge so it
                # cannot consume the resend notice and enqueue a synthetic continuation loop.
                logger.error(
                    "Rejecting turn for routing key %s on session %s after "
                    "turn-lease timeout; transcript load was not started and "
                    "the user must resend",
                    _quick_key, exc.session_id,
                )
                return t("gateway.busy.another_turn_running")
            try:
                await self._run_post_turn_hooks(
                    agent_result=_agent_result, source=source, is_internal=is_internal, event=event,
                )
            except Exception as _goal_exc:
                logger.debug("post-turn hook failed: %s", _goal_exc)
            return _agent_result
        finally:
            # One-shot restore (/moa, /model --once) must run on EVERY exit path (success,
            # exception, interrupt); the generation guard makes a displaced turn's finalizer a no-op.
            self._restore_pending_one_turn_model_override(_quick_key, _run_generation)
            # SIGKILL/OOM skips finally, leaving the durable marker for the next unclean startup's
            # recovery pass. A turn the adapter delivers hands its marker to that lifecycle, which
            # clears it only once the reply is in the delivery ledger (else a kill in between
            # left neither marker nor ledger row and the persisted reply was never sent).
            if not getattr(event, "_turn_marker_handoff", False):
                await self._clear_durable_active_turn(event)
            # Release only this turn's generation. Eviction may immediately admit a replacement
            # through the cold path; an unconditional release here would then clear the replacement
            # sentinel/agent and lease. Reset/stop release their stale slot before installing a
            # successor, preserving reset-zombie cleanup without granting gen-N successor authority.
            self._release_running_agent_state(_quick_key, run_generation=_run_generation)
            # Turn lease is keyed by (routing key, run generation) so this unwind can only free
            # the lease its own turn acquired, never a newer turn's.
            self._release_turn_lease(_quick_key, _run_generation)


class NativeStartup:
    async def _run_startup_resume_event(
        self, adapter: BasePlatformAdapter, event: MessageEvent, session_key: str,
    ) -> None:
        """Dispatch one synthetic startup resume and wait for its agent turn (inbound stays queued
        until it finishes, else a user message can race it)."""
        from gateway.run import _AGENT_PENDING_SENTINEL
        try:
            await adapter.handle_message(event)
            session_tasks = getattr(adapter, "_session_tasks", {})
            task = session_tasks.get(session_key) if isinstance(session_tasks, dict) else None
            if task is not None:
                await asyncio.shield(task)
        finally:
            # Release the pre-claimed slot if handle_message raised before _handle_message took ownership.
            _pre_state = self._peek_session_state(session_key)
            if (_pre_state.turn.agent if _pre_state else None) is _AGENT_PENDING_SENTINEL:
                self._release_running_agent_state(session_key)


