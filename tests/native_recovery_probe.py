"""Exercise patched native multi-session routing without a messaging account."""
import asyncio
import os
import signal
import subprocess
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace
from gateway.run import GatewayRunner
from gateway.session import SessionSource, Platform, build_session_key
from hermes_cli import render_recovery


async def main():
    runner = GatewayRunner()
    active_turns = 0
    peak_turns = 0
    async def counted_turn(**kwargs):
        nonlocal active_turns, peak_turns
        active_turns += 1
        peak_turns = max(peak_turns, active_turns)
        try:
            await asyncio.sleep(0.03)
            return kwargs['sequence']
        finally:
            active_turns -= 1
    runner._run_agent = counted_turn
    results = await asyncio.gather(*(
        runner._render_serialized_agent(sequence=index) for index in range(3)
    ))
    assert results == [0, 1, 2] and peak_turns == 1, 'Gateway ran multiple agent turns at once'

    sources = [SessionSource(platform=Platform.TELEGRAM, chat_id='ci-recovery',
        user_id='ci-user', chat_type='dm', thread_id=str(i)) for i in (101, 102, 103)]
    entries = [runner.session_store.get_or_create_session(source) for source in sources]
    ids = [entry.session_id for entry in entries]
    runner._session_db.enable_telegram_topic_mode(chat_id='ci-recovery', user_id='ci-user', has_topics_enabled=True, allows_users_to_create_topics=True)
    for source, entry in zip(sources, entries):
        runner._record_telegram_topic_binding(source, entry)
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
        assert any(msg.get('content') == 'Saved tool progress: chemistry notes drafted' for msg in kwargs['history']), 'Missing saved history'
        seen.append((kwargs['session_id'], kwargs['message']))
        return {'completed': True, 'final_response': 'Recovered task completed',
            'messages': [], 'api_calls': 1}
    runner._run_agent = model
    class HandlerAdapter:
        config = SimpleNamespace(extra={})
        async def send(self, *args, **kwargs):
            return SimpleNamespace(success=True, message_id='ci-recovery-response')
        async def handle_message(self, event):
            response = await runner._handle_message(event)
            assert response == 'Recovered task completed', response
    runner.adapters[Platform.TELEGRAM] = HandlerAdapter()
    for i, entry in enumerate(entries):
        render_recovery.begin(entry, SimpleNamespace(text=f'Real handler task {i}', internal=False))
        compressed = entry.session_id + '-compressed'
        runner._session_db.create_session(session_id=compressed, source='telegram', user_id='ci-user')
        render_recovery.rebind(runner.session_store, entry.session_key, compressed, runner._session_db)
        runner.session_store.append_to_transcript(compressed, {'role': 'user', 'content': f'Real handler task {i}'})
        runner.session_store.append_to_transcript(compressed, {'role': 'assistant', 'content': 'Saved tool progress: chemistry notes drafted'})
        entry.updated_at = datetime.now() - timedelta(days=3)
        entry.resume_pending = False
        runner.session_store._save()
        assert runner.session_store.get_or_create_session(sources[i]).session_id == compressed
        binding = runner._session_db.get_telegram_topic_binding(chat_id='ci-recovery', thread_id=sources[i].thread_id)
        assert binding['session_id'] == compressed
    runner._session_db.bind_telegram_topic(chat_id='ci-recovery', thread_id=sources[0].thread_id, user_id='ci-user', session_key=entries[0].session_key, session_id=ids[0])
    render_recovery.recover(runner.session_store)
    assert runner._schedule_resume_pending_sessions() == 3
    await asyncio.gather(*list(runner._background_tasks))
    assert len(seen) == 3
    for i, entry in enumerate(entries):
        assert any(sid == entry.session_id and f'Real handler task {i}' in text for sid, text in seen)
        assert not render_recovery.tracked(entry.session_key), 'Successful native handler must clear journal'
    # A hard kill never executes gateway shutdown/drain callbacks. A fresh
    # interpreter must find each unfinished lane from the persisted journal.
    child = subprocess.run([sys.executable, '-c', '''
import os, signal
from types import SimpleNamespace
from gateway.run import GatewayRunner
from gateway.session import SessionSource, Platform
from hermes_cli import render_recovery
runner = GatewayRunner()
for thread in ('201', '202', '203'):
    source = SessionSource(platform=Platform.TELEGRAM, chat_id='ci-hard-kill',
        user_id='ci-user', chat_type='dm', thread_id=thread)
    entry = runner.session_store.get_or_create_session(source)
    render_recovery.begin(entry, SimpleNamespace(text='Continue hard-killed work ' + thread, internal=False, source=source))
    runner.session_store.append_to_transcript(entry.session_id, {'role': 'assistant', 'content': 'Saved tool progress: chemistry notes drafted'})
runner.session_store._save()
os.kill(os.getpid(), signal.SIGKILL)
'''], timeout=30)
    assert child.returncode == -signal.SIGKILL
    runner = GatewayRunner()
    runner.session_store._ensure_loaded_locked()
    killed = [e for e in runner.session_store._entries.values()
              if e.origin and e.origin.chat_id == 'ci-hard-kill']
    assert len(killed) == 3
    # Partial/older session index: the task journal must rebuild its exact lane.
    orphan = killed[0]
    runner.session_store._entries.pop(orphan.session_key)
    runner.session_store._save()
    render_recovery.recover(runner.session_store)
    assert runner.session_store._entries[orphan.session_key].session_id == orphan.session_id
    seen.clear()
    runner._run_agent = model
    runner.adapters[Platform.TELEGRAM] = HandlerAdapter()
    assert runner._schedule_resume_pending_sessions() == 3
    await asyncio.gather(*list(runner._background_tasks))
    assert len(seen) == 3
    assert all('Continue hard-killed work' in message for _, message in seen)
    print('SIGKILL recovery passed for three lanes, including a missing session index entry')
    print('Three Telegram topic lanes recover with saved history; gateway agent turns stay serialized')


asyncio.run(main())
