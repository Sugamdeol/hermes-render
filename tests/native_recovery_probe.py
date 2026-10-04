"""Exercise patched native session routing without a real messaging account."""
import asyncio
from datetime import datetime, timedelta
import tempfile
from types import SimpleNamespace
from gateway.run import GatewayRunner
from gateway.session import SessionSource, Platform
from hermes_cli import render_recovery


async def main():
    runner = GatewayRunner()
    source = SessionSource(platform=Platform.TELEGRAM, chat_id='ci-recovery', user_id='ci-user')
    entry = runner.session_store.get_or_create_session(source)
    sid = entry.session_id
    # Long work is outside native crash detection's two-minute cutoff.
    entry.updated_at = datetime.now() - timedelta(hours=4)
    runner.session_store._save()
    render_recovery.begin(entry, SimpleNamespace(text='Finish the chemistry notes', internal=False))
    render_recovery.recover(runner.session_store)
    # Native lookup must preserve the same lane even after normal expiry.
    assert runner.session_store.get_or_create_session(source).session_id == sid
    events = []
    class Adapter:
        async def handle_message(self, event):
            events.append(event)
    runner.adapters[Platform.TELEGRAM] = Adapter()
    assert runner._schedule_resume_pending_sessions() == 1
    await asyncio.gather(*list(runner._background_tasks))
    assert len(events) == 1 and events[0].internal
    assert events[0].source.chat_id == source.chat_id
    assert 'Finish the chemistry notes' in events[0].text
    for _ in range(3):
        render_recovery.recover(runner.session_store)
    assert runner._schedule_resume_pending_sessions() == 0
    assert runner.session_store.get_or_create_session(source).session_id == sid
    render_recovery.finish(entry.session_key)
    runner.session_store.clear_resume_pending(entry.session_key)
    print('Native interrupted task auto-schedules in its original session; retry exhaustion preserves the lane')


asyncio.run(main())
