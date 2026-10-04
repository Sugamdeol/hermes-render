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
    # Retry a failed continuation in this same live runner after cooldown.
    with render_recovery._lock:
        data = render_recovery._read()
        for entry in entries:
            data[entry.session_key]['next_retry_at'] = 0
            data[entry.session_key]['attempts'] = 5
        render_recovery._write(data)
    watcher = asyncio.create_task(render_recovery.watch(runner, interval=0.05))
    await asyncio.sleep(0.25)
    watcher.cancel()
    await asyncio.gather(watcher, return_exceptions=True)
    await asyncio.gather(*list(runner._background_tasks))
    assert len(events) == 6, 'Live recovery must continue after more than three failures'
    for entry in entries:
        render_recovery.finish(entry.session_key)
        runner.session_store.clear_resume_pending(entry.session_key)
    # Exercise the real gateway handler; only model execution is stubbed.
    seen = []
    async def model(**kwargs):
        seen.append((kwargs['session_id'], kwargs['message']))
        return {'completed': True, 'final_response': 'Recovered task completed',
            'messages': [], 'api_calls': 1}
    runner._run_agent = model
    class HandlerAdapter:
        config = SimpleNamespace(extra={})
        async def handle_message(self, event):
            response = await runner._handle_message(event)
            assert response == 'Recovered task completed', response
    runner.adapters[Platform.TELEGRAM] = HandlerAdapter()
    for i, entry in enumerate(entries):
        render_recovery.begin(entry, SimpleNamespace(text=f'Real handler task {i}', internal=False))
    render_recovery.recover(runner.session_store)
    assert runner._schedule_resume_pending_sessions() == 3
    await asyncio.gather(*list(runner._background_tasks))
    assert len(seen) == 3
    for i, entry in enumerate(entries):
        assert any(sid == entry.session_id and f'Real handler task {i}' in text for sid, text in seen)
        assert not render_recovery.tracked(entry.session_key), 'Successful native handler must clear journal'
    print('Three original lanes recover through real gateway handler; live retries continue beyond three failures')


asyncio.run(main())
