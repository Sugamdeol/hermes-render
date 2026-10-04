"""Exercise patched native multi-session routing without a messaging account."""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from gateway.run import GatewayRunner
from gateway.session import SessionSource, Platform, build_session_key
from hermes_cli import render_recovery


async def main():
    runner = GatewayRunner()
    sources = [SessionSource(platform=Platform.TELEGRAM, chat_id='ci-recovery',
        user_id='ci-user', chat_type='group', thread_id=str(i)) for i in (101, 102, 103)]
    entries = [runner.session_store.get_or_create_session(source) for source in sources]
    ids = [entry.session_id for entry in entries]
    for i, entry in enumerate(entries):
        entry.updated_at = datetime.now() - timedelta(hours=4)
        render_recovery.begin(entry, SimpleNamespace(text=f'Finish notes {i}', internal=False))
    runner.session_store._save()
    # Restarting before dispatch must not exhaust waiting lanes.
    for _ in range(5):
        render_recovery.recover(runner.session_store)
    events = []
    gate = asyncio.Event()
    active = 0
    peak = 0
    class Adapter:
        config = SimpleNamespace(extra={})
        def __init__(self):
            self._session_tasks = {}
        async def handle_message(self, event):
            key = build_session_key(event.source)
            async def work():
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                events.append(event)
                try:
                    await gate.wait()
                    await asyncio.sleep(0.02)
                finally:
                    active -= 1
                    self._session_tasks.pop(key, None)
            self._session_tasks[key] = asyncio.create_task(work())
    adapter = Adapter()
    runner.adapters[Platform.TELEGRAM] = adapter
    assert runner._schedule_resume_pending_sessions() == 3
    assert runner._schedule_resume_pending_sessions() == 0, 'Duplicate scheduling'
    await asyncio.sleep(0.05)
    assert len(events) == 1, 'handle_message returning must not free the recovery slot'
    records = render_recovery._read()
    assert sum(records[entry.session_key]['attempts'] for entry in entries) == 1
    gate.set()
    await asyncio.gather(*list(runner._background_tasks))
    assert peak == 1 and len(events) == 3
    for i, (source, entry, sid) in enumerate(zip(sources, entries, ids)):
        assert runner.session_store.get_or_create_session(source).session_id == sid
        event = next(e for e in events if e.source.thread_id == source.thread_id)
        assert event.internal and f'Finish notes {i}' in event.text
        assert render_recovery._read()[entry.session_key]['attempts'] == 1
    # One repeatedly crashing lane must not exhaust the others.
    first = entries[0]
    for _ in range(2):
        render_recovery.recover(runner.session_store)
        assert render_recovery.claim(runner.session_store, first.session_key, first.session_id)
    render_recovery.recover(runner.session_store)
    assert first.resume_reason == 'render_recovery_paused'
    assert runner._schedule_resume_pending_sessions() == 2
    await asyncio.gather(*list(runner._background_tasks))
    assert runner.session_store.get_or_create_session(sources[0]).session_id == ids[0]
    for entry in entries:
        render_recovery.finish(entry.session_key)
        runner.session_store.clear_resume_pending(entry.session_key)
    print('All three native Telegram topic sessions recover serially in their original lanes; waiting retries preserved')


asyncio.run(main())
