#!/bin/sh
# Entrypoint wrapper for the render-tools image.
#
# Runs as root (PID-1 child of tini). On every boot it:
#   1. Ensures /opt/data exists and is owned by hermes:hermes.
#   2. Restores state from the GitHub state repo (the state branch is empty
#      on a first launch; seeding it is left to the git sync daemon, so
#      nothing here can delay the port bind).
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
GIT_SYNC="/opt/render-tools/git-storage.py"

# Which backend keeps the durable copy of ${DATA_DIR}?
#
#   git - a private GitHub repo, configured with GIT_STATE_REPO and a token.
#         It uploads only the bytes that changed (a few KB per save) instead
#         of a whole archive (tens of MB). Saves are change-driven: the
#         daemon fingerprints /opt/data every few seconds and pushes once the
#         tree settles. Without it there is no remote copy: /opt/data is
#         ephemeral on Free and state lives only for the current instance.
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
if ! chown -R hermes:hermes "${DATA_DIR}" 2>/dev/null; then
  echo "[render-tools] warning: could not chown ${DATA_DIR}; continuing" >&2
fi

# Restore a previous instance's state.
#
# GitHub is the durable store as soon as the state branch holds anything. The
# one exception is the very first launch, when that branch is still empty:
# there is nothing to restore, so the git sync daemon seeds GitHub from the
# tree this instance builds once the gateway is up.
#
# `state` is what makes this safe rather than a guess: it answers
# "has-state" / "empty" / "unknown", and only an *empty* branch is seeded --
# an unreachable one is not assumed empty, because seeding force-pushes and
# could otherwise overwrite newer GitHub state with an unproven local tree.
#
# Run the helpers as hermes so restored files already have the right owner.
#
# Every step below runs before the container binds a port, and Render fails a
# deploy whose service never opens one ("Port scan timeout reached, no open
# ports detected"). So each one is bounded and timed: a slow backend is allowed
# to cost this boot its restored state, never the deploy itself.
BOOT_START="$(date +%s)"
elapsed_boot_seconds() {
  echo $(( $(date +%s) - BOOT_START ))
}

# A hung download would otherwise hold the boot open indefinitely. Set to 0 to
# wait for the backend no matter how long it takes.
RESTORE_TIMEOUT="${HERMES_RESTORE_TIMEOUT_SECONDS:-240}"

run_bounded() {
  # $@ = command, run under `timeout` when one is configured and available.
  case "${RESTORE_TIMEOUT}" in
    ""|0|"0")
      "$@"
      ;;
    *)
      if command -v timeout >/dev/null 2>&1; then
        timeout "${RESTORE_TIMEOUT}" "$@"
      else
        "$@"
      fi
      ;;
  esac
}

STATE_SOURCE="fresh"
GIT_HAS_STATE=0
GIT_STATE_SEED_ON_BOOT=""
export GIT_STATE_SEED_ON_BOOT

if [ "${GIT_BACKEND}" -eq 1 ]; then
  git_state="$(gosu hermes "${GIT_SYNC}" state "${DATA_DIR}" 2>/dev/null | head -n 1)"
  case "${git_state}" in
    has-state)
      GIT_HAS_STATE=1
      if run_bounded gosu hermes "${GIT_SYNC}" restore "${DATA_DIR}"; then
        STATE_SOURCE="github"
      else
        # $? is the status of the condition, so 124 means `timeout` stopped it.
        restore_status=$?
        if [ "${restore_status}" -eq 124 ]; then
          echo "[render-tools] warning: state restore ran past ${RESTORE_TIMEOUT}s and was stopped," >&2
          echo "[render-tools] warning: so the port binds in time. Raise HERMES_RESTORE_TIMEOUT_SECONDS if this repeats." >&2
        fi
        echo "[render-tools] warning: github state restore failed; continuing without restored state" >&2
      fi
      ;;
    empty)
      echo "[render-tools] github state branch is empty (first launch); the sync daemon seeds it after boot" >&2
      ;;
    *)
      # Either the repo is unreachable, or the helper itself could not run.
      # Neither is proof that the branch is empty, so it is not seeded.
      echo "[render-tools] warning: could not read the github state repo; treating it as unavailable" >&2
      ;;
  esac
fi

echo "[render-tools] state restore finished in $(elapsed_boot_seconds)s (source=${STATE_SOURCE})"

# Whatever we just restored is not in GitHub yet: the state branch is empty, or
# GitHub could not be reached. Hand that to the sync daemon instead of pushing
# from here.
#
# This used to be a blocking `${GIT_SYNC} seed`. A first state push is the
# biggest upload this service ever makes, and when GitHub stalls on it --
# "RPC failed; HTTP 408 ... send-pack: unexpected disconnect", then a forced
# retry -- the boot sat there for minutes. That is exactly how a deploy dies
# with "Port scan timeout reached, no open ports detected": the dashboard never
# got to bind. The daemon seeds the branch ~15s after the gateway is up, and a
# failed attempt costs a retry on its next tick rather than a redeploy.
#
# The flag below is also the promise not to push a tree this instance never
# restored from GitHub over state the branch already holds.
if [ "${GIT_BACKEND}" -eq 1 ] && [ "${STATE_SOURCE}" != "github" ]; then
  GIT_STATE_SEED_ON_BOOT=1
  export GIT_STATE_SEED_ON_BOOT
  if [ "${GIT_HAS_STATE}" -eq 0 ]; then
    echo "[render-tools] github has no state yet; the sync daemon seeds it from this instance after boot"
  else
    # GitHub holds state we could not read, so this instance's /opt/data is not
    # derived from it. Pushing would replace the backup with a partial tree,
    # so hold off until a boot that restores cleanly.
    echo "[render-tools] warning: github holds state but it could not be restored;" >&2
    echo "[render-tools] warning: not pushing until a restart that restores it cleanly" >&2
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
# with the rest of /opt/data from the state repo, so later dashboard edits are
# not repeatedly overwritten.
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
  # The state backend also arbitrates the lease, so both instances are
  # reading the same source of truth. The git backend answers this with a
  # single `git ls-remote`, which transfers only a few hundred bytes.
  if [ "${GIT_BACKEND}" -eq 1 ]; then
    ROLE_SOURCE="${GIT_SYNC}"
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

# Keep a remote snapshot up to date in the background. The worker is started
# as hermes so it cannot read files outside the data directory. It is optional
# and exits cleanly when its credentials are not configured.
if [ "${GIT_BACKEND}" -eq 1 ]; then
  gosu hermes nice -n 10 "${GIT_SYNC}" daemon "${DATA_DIR}" &
  echo "[render-tools] started git state sync (delta uploads on change)"
fi

# Hand off to the upstream entrypoint. The upstream script handles
# privilege drop, dashboard backgrounding, and the actual gateway exec.
exec /opt/hermes/docker/entrypoint.sh "$@"
