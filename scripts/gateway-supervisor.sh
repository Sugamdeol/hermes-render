#!/bin/sh
# Keep the gateway recoverable without a resident Python supervisor.
while :; do
  setsid /opt/hermes/.venv/bin/python /opt/render-tools/agent-budget.py run gateway /opt/hermes/.venv/bin/hermes gateway run &
  worker_pid=$!
  wait "$worker_pid"
  status=$?
  kill -s KILL "-$worker_pid" 2>/dev/null || true
  echo "[render-tools] gateway exited ($status); restarting in 20s" >&2
  sleep 20
done
