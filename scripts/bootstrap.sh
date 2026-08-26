#!/bin/sh
# Entrypoint wrapper for the render-tools image.
#
# Runs as root (PID-1 child of tini). On every boot it:
#   1. Ensures /opt/data exists and is owned by hermes:hermes.
#   2. Restores optional state from GoFile.
#   3. Seeds the upstream config template when this is a fresh instance.
#   4. Runs the config patcher as the hermes user. On a fresh template it
#      lowers known upstream resource defaults; later boots preserve edits.
#      Integration entries remain idempotent and insert-only.
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

# Restore a previous instance's state when the remote-storage token is
# configured. Run the helper as hermes so extracted files already have the
# right owner and we avoid a second recursive chown of the whole data tree.
if [ -x "${STORAGE_SYNC}" ]; then
  if ! gosu hermes "${STORAGE_SYNC}" restore "${DATA_DIR}"; then
    STORAGE_RESTORE_OK=0
    echo "[render-tools] warning: remote state restore failed; continuing without state sync" >&2
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

# Apply the resource profile once per data tree. The marker is restored along
# with GoFile state, so later dashboard edits are not repeatedly overwritten.
RESOURCE_MARKER="${DATA_DIR}/.render-tools-free-profile-v1"
APPLY_FREE_DEFAULTS=0
if [ ! -f "${RESOURCE_MARKER}" ]; then
  APPLY_FREE_DEFAULTS=1
fi

# Patch config.yaml. We never fail the boot on a patch error — the agent
# can still run without the Render MCP server registered, and the user
# can always add it manually from the dashboard.
if [ -x "${PATCHER}" ]; then
  if ! RENDER_TOOLS_APPLY_FREE_DEFAULTS="${APPLY_FREE_DEFAULTS}" \
      gosu hermes "${PATCHER}" "${DATA_DIR}/config.yaml"; then
    echo "[render-tools] warning: config patch failed; continuing with unmodified config" >&2
  elif [ "${APPLY_FREE_DEFAULTS}" -eq 1 ]; then
    if touch "${RESOURCE_MARKER}" && chown hermes:hermes "${RESOURCE_MARKER}"; then
      echo "[render-tools] applied Free-tier resource profile"
    else
      echo "[render-tools] warning: could not write resource-profile marker" >&2
    fi
  fi
else
  echo "[render-tools] warning: ${PATCHER} not found or not executable; skipping" >&2
fi

# Keep a remote snapshot up to date in the background. The worker is started
# as hermes so it cannot read files outside the data directory. It is fully
# optional and exits cleanly when storage credentials are not configured.
if [ -x "${STORAGE_SYNC}" ] && [ "${STORAGE_RESTORE_OK}" -eq 1 ]; then
  # Backups are deliberately lower priority than the gateway. The worker is
  # change-aware, so quiet instances do not repeatedly recompress /opt/data.
  gosu hermes nice -n 10 "${STORAGE_SYNC}" daemon "${DATA_DIR}" &
  echo "[render-tools] started optional low-priority remote state sync"
fi

# Pre-flight TELEGRAM_BOT_TOKEN when it is configured. A revoked or mistyped
# token makes the upstream gateway exit(1) at startup — and as the foreground
# process that takes the whole container (dashboard included) down with it,
# restarting straight into the same failure. The checker asks Telegram's
# getMe endpoint and, only on an authoritative 401/404, exits 3 so we drop
# the platform for this boot and the service stays up and fixable from the
# dashboard. Transient network errors leave the token untouched. See
# scripts/check-telegram-token.py for the full exit-code contract.
TOKEN_CHECK="/opt/render-tools/check-telegram-token.py"
if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -x "${TOKEN_CHECK}" ]; then
  TOKEN_CHECK_STATUS=0
  TOKEN_CHECK_STDOUT="$(gosu hermes "${TOKEN_CHECK}")" || TOKEN_CHECK_STATUS=$?
  case "${TOKEN_CHECK_STATUS}" in
    0) ;;
    3)
      # Telegram definitively rejected the token; the checker already
      # printed remediation steps. Run the gateway platform-less this boot.
      unset TELEGRAM_BOT_TOKEN
      echo "[render-tools] starting the gateway without Telegram for this boot" >&2
      ;;
    4)
      # The raw value had whitespace damage; the checker verified the
      # trimmed token and printed it on stdout. Re-export the clean value.
      if [ -n "${TOKEN_CHECK_STDOUT}" ]; then
        TELEGRAM_BOT_TOKEN="${TOKEN_CHECK_STDOUT}"
        export TELEGRAM_BOT_TOKEN
        echo "[render-tools] re-exported the trimmed TELEGRAM_BOT_TOKEN" >&2
      else
        echo "[render-tools] warning: token pre-flight returned no cleaned value; continuing unchanged" >&2
      fi
      ;;
    *)
      echo "[render-tools] warning: token pre-flight exited ${TOKEN_CHECK_STATUS}; continuing with TELEGRAM_BOT_TOKEN unchanged" >&2
      ;;
  esac
fi

# Hand off to the upstream entrypoint. The upstream script handles
# privilege drop, dashboard backgrounding, and the actual gateway exec.
exec /opt/hermes/docker/entrypoint.sh "$@"
