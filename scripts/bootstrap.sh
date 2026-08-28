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
GIT_SYNC="/opt/render-tools/git-storage.py"

# Which backend keeps the durable copy of ${DATA_DIR}?
#
#   git    - a private GitHub repo. Primary when GIT_STATE_REPO and a token are
#            set, because it uploads only the bytes that changed (a few KB per
#            save) instead of a whole archive (tens of MB).
#   gofile - the original whole-archive upload. Kept as a fallback: it still
#            holds older snapshots, and it works if GitHub is unreachable.
#
# Both may run at once. When git is primary, GoFile is slowed to a long
# interval (GOFILE_FALLBACK_INTERVAL_SECONDS, default 6 hours) so the pair
# together still fits a small monthly transfer allowance.
#
# Note: these must be real environment variables (Render's Environment tab),
# not values from env/common.env -- restore runs before the repo env is merged.
GIT_BACKEND=0
if [ -x "${GIT_SYNC}" ] && [ -n "${GIT_STATE_REPO:-}" ] \
   && { [ -n "${GIT_STATE_TOKEN:-}" ] || [ -n "${GITHUB_TOKEN:-}" ]; }; then
  GIT_BACKEND=1
fi

# Make sure the data dir exists and the hermes user can write to it
# before we run the patcher. Idempotent — on Free this is the ephemeral
# directory from the image; paid deployments may mount a disk here.
mkdir -p "${DATA_DIR}"
STORAGE_RESTORE_OK=1
if ! chown -R hermes:hermes "${DATA_DIR}" 2>/dev/null; then
  echo "[render-tools] warning: could not chown ${DATA_DIR}; continuing" >&2
fi

# Restore a previous instance's state. Try the git backend first when it is
# configured; fall back to the GoFile archive if git has nothing for us yet
# (first run after switching backends) or is unreachable. Run the helpers as
# hermes so restored files already have the right owner.
STORAGE_RESTORED=0
if [ "${GIT_BACKEND}" -eq 1 ]; then
  if gosu hermes "${GIT_SYNC}" restore "${DATA_DIR}"; then
    STORAGE_RESTORED=1
  else
    echo "[render-tools] notice: no state restored from the git repo; trying GoFile" >&2
  fi
fi

if [ "${STORAGE_RESTORED}" -eq 0 ] && [ -x "${STORAGE_SYNC}" ]; then
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

# Merge repo-managed environment into the runtime environment before the
# patcher runs, so config.yaml substitutions like ${RENDER_MCP_API_KEY} can
# resolve from a committed secret as well as from Render's Environment tab.
#
# Precedence (highest first): the live process environment, then the existing
# $HERMES_HOME/.env, then the repo. Set RENDER_TOOLS_SECRETS_FORCE=1 to let the
# repo win over .env, which is what a key rotation in git wants.
#
# Values are never echoed. The decrypted file lives in a private tmpfs-ish
# temp dir owned by hermes and is removed immediately after the merge.
SEEDER="/opt/render-tools/seed-env.py"
COMMON_ENV="/opt/render-tools/env/common.env"
SECRETS_ENC="/opt/render-tools/env/secrets.enc.env"

seed_from_file() {
  # $1 = plaintext dotenv path. Emits exports on stdout for eval by the caller.
  seed_args="--secrets $1 --env-file ${DATA_DIR}/.env --print-exports"
  if [ "${RENDER_TOOLS_SECRETS_FORCE:-0}" = "1" ]; then
    seed_args="${seed_args} --force"
  fi
  # shellcheck disable=SC2086
  gosu hermes "${SEEDER}" ${seed_args}
}

if [ -x "${SEEDER}" ] && [ -f "${COMMON_ENV}" ]; then
  if exports="$(seed_from_file "${COMMON_ENV}" 2>/dev/null)"; then
    eval "${exports}"
    unset exports
  else
    echo "[render-tools] warning: could not merge env/common.env; continuing" >&2
  fi
fi

if [ -x "${SEEDER}" ] && [ -f "${SECRETS_ENC}" ]; then
  if [ -z "${SOPS_AGE_KEY:-}" ] && [ -z "${SOPS_AGE_KEY_FILE:-}" ] \
     && [ ! -f /etc/secrets/age.key ]; then
    echo "[render-tools] encrypted secrets present but no age key (SOPS_AGE_KEY);" >&2
    echo "[render-tools] skipping decryption and using the process environment only" >&2
  elif ! command -v sops >/dev/null 2>&1; then
    echo "[render-tools] warning: sops not installed; skipping encrypted secrets" >&2
  else
    # A Render Secret File is the other supported way to supply the key.
    if [ -z "${SOPS_AGE_KEY:-}" ] && [ -z "${SOPS_AGE_KEY_FILE:-}" ] \
       && [ -f /etc/secrets/age.key ]; then
      SOPS_AGE_KEY_FILE=/etc/secrets/age.key
      export SOPS_AGE_KEY_FILE
    fi
    SECRETS_TMP="$(mktemp -d)"
    chown hermes:hermes "${SECRETS_TMP}"
    chmod 0700 "${SECRETS_TMP}"
    if gosu hermes env \
         SOPS_AGE_KEY="${SOPS_AGE_KEY:-}" \
         SOPS_AGE_KEY_FILE="${SOPS_AGE_KEY_FILE:-}" \
         sops --decrypt --input-type dotenv --output-type dotenv \
           --output "${SECRETS_TMP}/secrets.env" "${SECRETS_ENC}" 2>/dev/null; then
      if exports="$(seed_from_file "${SECRETS_TMP}/secrets.env")"; then
        eval "${exports}"
        unset exports
      else
        echo "[render-tools] warning: could not merge decrypted secrets; continuing" >&2
      fi
    else
      echo "[render-tools] warning: sops could not decrypt ${SECRETS_ENC}" >&2
      echo "[render-tools] check that SOPS_AGE_KEY matches the recipient in .sops.yaml" >&2
    fi
    rm -rf "${SECRETS_TMP}"
    unset SECRETS_TMP
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

# Install the image-managed dashboard plugin(s) into $HERMES_HOME/plugins/,
# where the dashboard scans for dashboard/manifest.json. Image wins over any
# restored or local copy on every boot (same convention as the Bynara
# provider entry and the baked skill bundles). The dashboard's "Add API
# provider" card on the Models page is provided by render-api-providers.
# Runs after state restore (a restored copy must not shadow the image) and
# before the sync daemon starts (the image copy is what gets backed up).
PLUGINS_SRC="/opt/render-tools/dashboard-plugins"
PLUGINS_DST="${DATA_DIR}/plugins"
if [ -d "${PLUGINS_SRC}" ]; then
  if mkdir -p "${PLUGINS_DST}" 2>/dev/null && chown hermes:hermes "${PLUGINS_DST}" 2>/dev/null; then
    plugin_install_ok=1
    for p in "${PLUGINS_SRC}"/*/; do
      [ -d "${p}" ] || continue
      plugin_name="$(basename "${p}")"
      # Replace any restored/local copy wholesale so the bundled version
      # always wins, and drop stale files from older image versions.
      if ! gosu hermes rm -rf "${PLUGINS_DST}/${plugin_name}" \
        || ! gosu hermes cp -rf "${p}" "${PLUGINS_DST}/"; then
        plugin_install_ok=0
      fi
    done
    if [ "${plugin_install_ok}" -eq 1 ]; then
      echo "[render-tools] installed dashboard plugin(s) into ${PLUGINS_DST}"
    else
      echo "[render-tools] warning: dashboard plugin install failed; continuing without" >&2
    fi
  else
    echo "[render-tools] warning: could not prepare ${PLUGINS_DST}; dashboard plugins stay image-baked" >&2
  fi
fi

# Failover role. When several instances share a state backend (typically this
# Render service plus a laptop), the highest-priority one with a fresh lease is
# ACTIVE and the others are STANDBY. A standby keeps its dashboard, but must
# not talk to chat platforms -- otherwise both agents answer the same Telegram
# message -- so we strip the channel tokens from its environment here, before
# the gateway ever sees them.
#
# Failing open is deliberate: any error leaves the instance ACTIVE, so a
# coordination outage can never leave you with no agent answering at all.
HERMES_ROLE=active
ROLE_SOURCE=""
if [ "${HERMES_FAILOVER:-0}" = "1" ]; then
  # Whichever backend is primary also arbitrates the lease, so both instances
  # are reading the same source of truth. The git backend answers this with a
  # single `git ls-remote`, which transfers only a few hundred bytes.
  if [ "${GIT_BACKEND}" -eq 1 ]; then
    ROLE_SOURCE="${GIT_SYNC}"
  elif [ -x "${STORAGE_SYNC}" ]; then
    ROLE_SOURCE="${STORAGE_SYNC}"
  fi
fi
if [ -n "${ROLE_SOURCE}" ]; then
  if role_output="$(gosu hermes "${ROLE_SOURCE}" role "${DATA_DIR}" 2>/dev/null)"; then
    case "${role_output}" in
      standby) HERMES_ROLE=standby ;;
      *) HERMES_ROLE=active ;;
    esac
  else
    echo "[render-tools] warning: could not determine failover role; staying active" >&2
  fi
  export HERMES_ROLE
  if [ "${HERMES_ROLE}" = "standby" ]; then
    echo "[render-tools] role=standby: another instance holds the lease." >&2
    echo "[render-tools] chat platforms disabled here; dashboard stays available" >&2
    unset TELEGRAM_BOT_TOKEN DISCORD_BOT_TOKEN SLACK_BOT_TOKEN SLACK_APP_TOKEN \
          SIGNAL_PHONE_NUMBER EMAIL_ADDRESS EMAIL_PASSWORD EMAIL_IMAP_HOST \
          EMAIL_SMTP_HOST 2>/dev/null || true
  else
    echo "[render-tools] role=active: this instance handles messages" >&2
  fi
fi

# Keep a remote snapshot up to date in the background. The workers are started
# as hermes so they cannot read files outside the data directory. Both are
# optional and exit cleanly when their credentials are not configured.
if [ "${GIT_BACKEND}" -eq 1 ]; then
  gosu hermes nice -n 10 "${GIT_SYNC}" daemon "${DATA_DIR}" &
  echo "[render-tools] started git state sync (primary, delta uploads)"
fi

if [ -x "${STORAGE_SYNC}" ] && [ "${STORAGE_RESTORE_OK}" -eq 1 ]; then
  # Backups are deliberately lower priority than the gateway. The worker is
  # change-aware, so quiet instances do not repeatedly recompress /opt/data.
  #
  # When git is primary, GoFile becomes an occasional belt-and-braces copy.
  # Each GoFile run re-uploads the whole archive, so leaving it at its normal
  # cadence would spend the monthly transfer allowance that switching to git
  # was meant to save.
  if [ "${GIT_BACKEND}" -eq 1 ]; then
    GOFILE_MIN_UPLOAD_INTERVAL_SECONDS="${GOFILE_FALLBACK_INTERVAL_SECONDS:-21600}"
    export GOFILE_MIN_UPLOAD_INTERVAL_SECONDS
    # The git backend owns the failover lease; a second voter would only
    # confuse the decision.
    HERMES_FAILOVER=0 gosu hermes nice -n 10 "${STORAGE_SYNC}" daemon "${DATA_DIR}" &
    echo "[render-tools] started GoFile state sync (fallback, every ${GOFILE_MIN_UPLOAD_INTERVAL_SECONDS}s)"
  else
    gosu hermes nice -n 10 "${STORAGE_SYNC}" daemon "${DATA_DIR}" &
    echo "[render-tools] started optional low-priority remote state sync"
  fi
fi

# Hand off to the upstream entrypoint. The upstream script handles
# privilege drop, dashboard backgrounding, and the actual gateway exec.
exec /opt/hermes/docker/entrypoint.sh "$@"
