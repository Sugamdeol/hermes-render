"""Resume durable UI turn markers when a capable dashboard/TUI client reconnects."""
from functools import wraps
import logging
import os
from pathlib import Path
import threading
import time

LOG = logging.getLogger(__name__)


def marker_candidates(home, markers):
    return [(key, row) for key, row in markers._load(markers._marker_path(home)).items()
            if isinstance(row, dict) and row.get('auto_continue', True)
            and str(row.get('prompt') or '').strip()
            and markers.marker_writer_state(row) == 'dead']


def resume_one(server, key):
    # The native handler checks the session row, profile, writer ownership,
    # compression lineage and stored model before scheduling its continuation.
    db = server._get_db()
    if db is None or db.get_session(key) is None:
        return False  # Do not resurrect a deleted transcript from a leftover marker.
    result = server._methods['session.resume'](
        '__colab_recovery__', {'session_id':key, 'omit_messages':True})
    if not isinstance(result, dict) or result.get('error'):
        return False
    payload = result.get('result') or {}
    sid = payload.get('session_id')
    if not sid or not payload.get('auto_continue'):
        return False
    # Native resume schedules a background build/turn. Hold the recovery slot
    # through both phases so the next batch cannot allocate extra agents early.
    while True:
        session = server._sessions.get(sid)
        if session is None:
            break
        if not session.get('_auto_continue_scheduled') and not session.get('running'):
            break
        time.sleep(.5)
    return True


def install(server):
    if os.environ.get('HERMES_RUNTIME_PROFILE') != 'colab-full':
        return False
    if os.environ.get('HERMES_RESUME_ALL_INTERRUPTED', '1') != '1':
        return False
    if getattr(server, '_colab_ui_recovery_installed', False):
        return False
    from hermes_cli import render_recovery
    from tui_gateway import turn_marker
    from tui_gateway.transport import current_transport
    required = ('_auto_continue_config', '_start_session_work')
    if any(not callable(getattr(server, name, None)) for name in required) or any(
            name not in server._methods for name in ('client.capabilities', 'session.resume')):
        raise RuntimeError('Native UI recovery hooks changed')
    original_config = server._auto_continue_config

    def config():
        enabled, _age, attempts = original_config()
        return enabled, float('inf'), max(attempts, 10)

    server._auto_continue_config = config
    # Only unfinished markers remain here: native completion, stop and cancel
    # retire their markers. Age/count pruning would erase work before reconnect.
    turn_marker._prune = lambda entries, now:dict(entries)
    original_capabilities = server._methods['client.capabilities']
    lock = threading.Lock()
    peers, in_flight = set(), set()
    slots = threading.Semaphore(render_recovery.session_concurrency())

    @wraps(original_capabilities)
    def capabilities(rid, params):
        result = original_capabilities(rid, params)
        if (not params.get('server_requests') or not server._auto_continue_config()[0]
                or (isinstance(result, dict) and result.get('error'))):
            return result
        peer = current_transport()
        if peer is None:
            return result
        identity = id(peer)
        with lock:
            if identity in peers:
                return result
            peers.add(identity)

        def run_one(key):
            try:
                if not getattr(peer, 'closed', False):
                    resume_one(server, key)
            except Exception:
                LOG.exception('UI interrupted-chat recovery failed; retrying on next scan')
            finally:
                slots.release()
                with lock:
                    in_flight.discard(key)

        def scan():
            try:
                while not getattr(peer, 'closed', False):
                    home = Path(server._hermes_home)
                    for key, _row in marker_candidates(home, turn_marker):
                        with lock:
                            if key in in_flight:
                                continue
                            in_flight.add(key)
                        slots.acquire()
                        if getattr(peer, 'closed', False):
                            slots.release()
                            with lock:
                                in_flight.discard(key)
                            break
                        # Native context threads retain this connected client's
                        # transport/profile. Approval requests keep their normal UI.
                        try:
                            worker = server._start_session_work(lambda key=key:run_one(key), name='colab-ui-resume')
                        except Exception:
                            slots.release()
                            with lock:
                                in_flight.discard(key)
                            raise
                        if worker is None:
                            slots.release()
                            with lock:
                                in_flight.discard(key)
                    time.sleep(10)
            except Exception:
                LOG.exception('UI recovery scan stopped; reconnect to retry')
            finally:
                with lock:
                    peers.discard(identity)

        try:
            if server._start_session_work(scan, name='colab-ui-recovery') is None:
                with lock:
                    peers.discard(identity)
        except Exception:
            with lock:
                peers.discard(identity)
            LOG.exception('UI recovery could not start; reconnect to retry')
        return result

    server._methods['client.capabilities'] = capabilities
    server._colab_ui_recovery_installed = True
    return True
