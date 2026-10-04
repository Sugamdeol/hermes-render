#!/bin/sh
# Keep the gateway recoverable without a resident Python supervisor. This
# script is the container's foreground command, so if it is OOM-killed the
# container exits and Render can replace the whole instance.
PYTHON="${HERMES_PYTHON:-/opt/hermes/.venv/bin/python}"
AGENT_BUDGET="${HERMES_AGENT_BUDGET_SCRIPT:-/opt/render-tools/agent-budget.py}"
HERMES_BIN="${HERMES_BIN:-/opt/hermes/.venv/bin/hermes}"
RESTART_DELAY="${HERMES_GATEWAY_RESTART_DELAY_SECONDS:-20}"
worker_pid=""
stopping=0

stop_supervisor() {
  stopping=1
  if [ -n "$worker_pid" ]; then
    kill -TERM -- "-$worker_pid" 2>/dev/null || true
  fi
}

trap 'stop_supervisor' INT TERM

while [ "$stopping" -eq 0 ]; do
  setsid "$PYTHON" "$AGENT_BUDGET" run gateway "$HERMES_BIN" gateway run &
  worker_pid=$!
  wait "$worker_pid"
  status=$?
  kill -s KILL "-$worker_pid" 2>/dev/null || true
  worker_pid=""
  [ "$stopping" -eq 0 ] || exit 0
  echo "[render-tools] gateway exited ($status); restarting in 20s" >&2
  sleep "$RESTART_DELAY"
done
