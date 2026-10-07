#!/bin/sh
# Recover a crashed dashboard independently of the messaging gateway.
HERMES_BIN="${HERMES_BIN:-/opt/hermes/.venv/bin/hermes}"
RESTART_DELAY="${HERMES_DASHBOARD_RESTART_DELAY_SECONDS:-5}"
worker_pid=""
stopping=0
stop_dashboard() {
  stopping=1
  if [ -n "$worker_pid" ]; then
    kill -TERM "-$worker_pid" 2>/dev/null || true
  fi
}
trap 'stop_dashboard' INT TERM
while [ "$stopping" -eq 0 ]; do
  setsid "$HERMES_BIN" dashboard "$@" &
  worker_pid=$!
  wait "$worker_pid"
  status=$?
  kill -s KILL "-$worker_pid" 2>/dev/null || true
  worker_pid=""
  [ "$stopping" -eq 0 ] || exit 0
  echo "[dashboard] server exited ($status); restarting in ${RESTART_DELAY}s" >&2
  sleep "$RESTART_DELAY"
done
