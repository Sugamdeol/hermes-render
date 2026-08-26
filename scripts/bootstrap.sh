#!/bin/sh
# Entrypoint wrapper for the render-tools image.
#
# Runs as root (PID-1 child of tini). On every boot it:
#   1. Ensures /opt/data exists and is owned by hermes:hermes.
#   2. Restores optional state from S3-compatible storage.
#   3. Seeds the upstream config template when this is a fresh instance.
#   4. Runs the config patcher as the hermes user. The patcher is
#      idempotent: it only INSERTs integration entries; it never overwrites
#      user edits.
#   5. Starts the optional state-sync worker and exec's the upstream
#      entrypoint chain with the original args (default CMD is `gateway run`).
#
# The upstream entrypoint also chowns /opt/data and drops to the hermes
# user via gosu for the gateway process. Our chown here is redundant in
# the happy path but harmless, and it lets the patcher run on a fresh
# disk that hasn't been chowned yet.

set -eu

DATA_DIR="${HERMES_HOME:-/opt/data}"
PATCHER="/opt/render-tools/patch-config.py"
STORAGE_SYNC="/opt/render-tools/free-storage.py"

# Make sure the data dir exists and the hermes user can write to it
# before we run the patcher. Idempotent — on Free this is the ephemeral
# directory from the image; paid deployments may mount a disk here.
mkdir -p "${DATA_DIR}"
STORAGE_RESTORE_OK=1
if ! chown -R hermes:hermes "${DATA_DIR}" 2>/dev/null; then
  echo "[render-tools] warning: could not chown ${DATA_DIR}; continuing" >&2
fi

# Restore a previous instance's state when all remote-storage credentials are
# configured. The helper treats missing credentials or a missing remote object
# as a normal no-op, and never prevents Hermes from starting.
if [ -x "${STORAGE_SYNC}" ]; then
  if ! "${STORAGE_SYNC}" restore "${DATA_DIR}"; then
    STORAGE_RESTORE_OK=0
    echo "[render-tools] warning: remote state restore failed; continuing without state sync" >&2
  fi
  # Files extracted from the archive are owned by root because bootstrap runs
  # as root. Give the Hermes process access before the patcher runs.
  if ! chown -R hermes:hermes "${DATA_DIR}" 2>/dev/null; then
    echo "[render-tools] warning: could not chown restored state; continuing" >&2
  fi
fi

# The upstream entrypoint seeds config.yaml later in its own setup phase.
# Seed it here first so the patcher does not turn a fresh install into a
# minimal config containing only the Render additions.
CONFIG_FILE="${DATA_DIR}/config.yaml"
UPSTREAM_CONFIG="/opt/hermes/cli-config.yaml.example"
if [ ! -f "${CONFIG_FILE}" ] && [ -f "${UPSTREAM_CONFIG}" ]; then
  if cp "${UPSTREAM_CONFIG}" "${CONFIG_FILE}" && chown hermes:hermes "${CONFIG_FILE}"; then
    echo "[render-tools] seeded ${CONFIG_FILE} from the upstream template"
  else
    echo "[render-tools] warning: could not seed ${CONFIG_FILE}; continuing" >&2
  fi
fi

# Patch config.yaml. We never fail the boot on a patch error — the agent
# can still run without the Render MCP server registered, and the user
# can always add it manually from the dashboard.
if [ -x "${PATCHER}" ]; then
  if ! gosu hermes "${PATCHER}" "${DATA_DIR}/config.yaml"; then
    echo "[render-tools] warning: config patch failed; continuing with unmodified config" >&2
  fi
else
  echo "[render-tools] warning: ${PATCHER} not found or not executable; skipping" >&2
fi

# Keep a remote snapshot up to date in the background. The worker is started
# as hermes so it cannot read files outside the data directory. It is fully
# optional and exits cleanly when storage credentials are not configured.
if [ -x "${STORAGE_SYNC}" ] && [ "${STORAGE_RESTORE_OK}" -eq 1 ]; then
  gosu hermes "${STORAGE_SYNC}" daemon "${DATA_DIR}" &
  echo "[render-tools] started optional remote state sync"
fi

# Hand off to the upstream entrypoint. The upstream script handles
# privilege drop, dashboard backgrounding, and the actual gateway exec.
exec /opt/hermes/docker/entrypoint.sh "$@"
