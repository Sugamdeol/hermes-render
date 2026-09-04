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
#   5. Starts the optional state-sync worker (supervised: if the worker
#      process ever dies, it is restarted) and the optional Free-tier
#      keep-alive ping, plus a health loop that restarts the dashboard
#      side-process if it dies and logs memory pressure.
#   6. Runs the upstream entrypoint chain as a supervised child. An
#      unexpected gateway exit is restarted in place (bounded by
#      HERMES_ENTRYPOINT_RESTARTS) instead of paying a full container
#      restart cycle; clean stops and an exhausted budget fall through to
#      the original path: hold the container open for a few bounded seconds
#      so the sync daemon can land its final push before tini tears down.
#
# The upstream entrypoint also chowns /opt/data and drops to the hermes
# user via gosu for the gateway process. Our chown here is redundant in
# the happy path but harmless, and it lets the patcher run on a fresh
# disk that hasn't been chowned yet.

set -eu

DATA_DIR="${HERMES_HOME:-/opt/data}"
# The helper paths are overridable for dev/test runs of this script; the
# image always uses the baked-in defaults.
PATCHER="${HERMES_PATCHER:-/opt/render-tools/patch-config.py}"
GIT_SYNC="${HERMES_GIT_SYNC:-/opt/render-tools/git-storage.py}"

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
  # Bounded for the same reason the restore is: `state` answers "is there
  # anything to restore?" by cloning the branch (a branch that exists is not
  # proof it holds state), so on every boot after the first this is a real
  # transfer on the pre-port-bind path. An empty answer means "unavailable",
  # which the case below already handles without seeding.
  git_state="$(run_bounded gosu hermes "${GIT_SYNC}" state "${DATA_DIR}" 2>/dev/null | head -n 1)"
  case "${git_state}" in
    has-state)
      GIT_HAS_STATE=1
      # A single transient GitHub failure must not disarm state sync for the
      # whole runtime ("not pushing until a restart that restores it
      # cleanly"), so a failed restore is retried while boot budget remains.
      # Each attempt is individually bounded by RESTORE_TIMEOUT; the retry
      # only happens when the boot is still fast enough to keep the port
      # scan happy.
      restore_attempts="${HERMES_RESTORE_ATTEMPTS:-2}"
      [ "${restore_attempts}" -ge 1 ] 2>/dev/null || restore_attempts=1
      restore_attempt=1
      while :; do
        if run_bounded gosu hermes "${GIT_SYNC}" restore "${DATA_DIR}"; then
          STATE_SOURCE="github"
          break
        fi
        # $? is the status of the condition, so 124 means `timeout` stopped it.
        restore_status=$?
        if [ "${restore_status}" -eq 124 ]; then
          echo "[render-tools] warning: state restore ran past ${RESTORE_TIMEOUT}s and was stopped," >&2
          echo "[render-tools] warning: so the port binds in time. Raise HERMES_RESTORE_TIMEOUT_SECONDS if this repeats." >&2
        fi
        if [ "${restore_attempt}" -ge "${restore_attempts}" ] \
           || [ "$(elapsed_boot_seconds)" -ge 180 ]; then
          echo "[render-tools] warning: github state restore failed; continuing without restored state" >&2
          break
        fi
        restore_attempt=$((restore_attempt + 1))
        echo "[render-tools] warning: state restore attempt failed; retrying (${restore_attempt}/${restore_attempts})" >&2
        sleep 3
      done
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
# Two sources, handled differently on purpose:
#
#   env/common.env      Non-secret deploy knobs (ports, HERMES_DASHBOARD_TUI,
#                       thread caps, cache sizes). Exported for the gateway
#                       and dashboard, but kept OUT of $HERMES_HOME/.env --
#                       and evicted from it if an older boot put them there.
#                       Upstream loads that file with override=True on every
#                       start, so a knob persisted in it would silently beat
#                       Render's Environment tab: a stale HERMES_DASHBOARD_TUI=0
#                       there is exactly why the Chat tab stayed off (and /chat
#                       bounced to Sessions) after the operator set it to 1.
#   env/secrets.enc.env Credentials. Merged into $HERMES_HOME/.env so the
#                       dashboard's API Keys tab sees them.
#
# Precedence for secrets (highest first): the live process environment, then
# the existing $HERMES_HOME/.env, then the repo. Set RENDER_TOOLS_SECRETS_FORCE=1
# to let the repo win over .env, which is what a key rotation in git wants.
# Knobs: the live process environment, then the repo; .env has no say.
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

knobs_from_file() {
  # $1 = plaintext dotenv of non-secret knobs. Emits exports on stdout; also
  # scrubs any copy of those keys out of ${DATA_DIR}/.env (see above).
  gosu hermes "${SEEDER}" --knobs "$1" --env-file "${DATA_DIR}/.env" --print-exports
}

if [ -x "${SEEDER}" ] && [ -f "${COMMON_ENV}" ]; then
  if exports="$(knobs_from_file "${COMMON_ENV}")"; then
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
PLUGINS_SRC="${HERMES_PLUGINS_SRC:-/opt/render-tools/dashboard-plugins}"
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
#
# The worker runs under a tiny supervisor loop: if the daemon *process* ever
# dies unexpectedly (an unexpected crash, a killed child, anything outside the
# exception guard inside the daemon itself), it is restarted here. A dead
# daemon that nobody restarts is exactly how "github files are not synced"
# happens -- the logs keep saying everything is fine until the next restart.
GIT_DAEMON_PIDFILE="${TMPDIR:-/tmp}/render-tools-git-daemon.pid"
GIT_DAEMON_SUPERVISOR=""
if [ "${GIT_BACKEND}" -eq 1 ]; then
  rm -f "${GIT_DAEMON_PIDFILE}" 2>/dev/null || true
  (
    daemon_attempts=0
    max_restarts="${HERMES_SYNC_DAEMON_RESTARTS:-20}"
    while :; do
      gosu hermes nice -n 10 "${GIT_SYNC}" daemon "${DATA_DIR}" &
      daemon_pid=$!
      echo "${daemon_pid}" > "${GIT_DAEMON_PIDFILE}" 2>/dev/null || true
      daemon_status=0
      wait "${daemon_pid}" || daemon_status=$?
      # 0 = the daemon exited cleanly -- it installs a SIGTERM handler and
      # returns normally, so a stop (container shutdown or a failover
      # handoff) looks like status 0, not 143; 143/130 = killed by
      # SIGTERM/SIGINT before the handler ran. All of those are normal
      # stops, not crashes -- do not restart.
      if [ "${daemon_status}" -eq 0 ] || [ "${daemon_status}" -eq 143 ] \
         || [ "${daemon_status}" -eq 130 ]; then
        exit 0
      fi
      daemon_attempts=$((daemon_attempts + 1))
      if [ "${daemon_attempts}" -gt "${max_restarts}" ]; then
        echo "[render-tools] warning: sync daemon keeps exiting; giving up after ${max_restarts} restarts" >&2
        exit 1
      fi
      echo "[render-tools] warning: sync daemon exited (status ${daemon_status});" \
          "restarting (${daemon_attempts}/${max_restarts})" >&2
      sleep 5
    done
  ) &
  GIT_DAEMON_SUPERVISOR=$!
  echo "[render-tools] started git state sync (delta uploads on change)"
fi

# Keep the Free service awake. Render spins a Free web service down after
# ~15 minutes without inbound HTTP traffic, and chat platforms are
# outbound-only -- so with no help the container sleeps mid-conversation and
# the agent simply stops answering until something happens to wake it.
#
# Render injects RENDER_EXTERNAL_URL into every web service; a tiny loop in
# the container requesting the dashboard's own /api/status over that public
# URL counts as traffic, which is all the spin-down timer looks at. It only
# runs on Render (no external URL, no loop), costs one small request per
# interval, and is disabled with HERMES_KEEP_ALIVE=0.
KEEP_ALIVE_URL="${RENDER_EXTERNAL_URL:-}"
if [ "${HERMES_KEEP_ALIVE:-1}" = "1" ] && [ -n "${KEEP_ALIVE_URL}" ]; then
  keep_alive_interval="${HERMES_KEEP_ALIVE_SECONDS:-600}"
  case "${keep_alive_interval}" in
    ''|*[!0-9]*) keep_alive_interval=600 ;;
  esac
  (
    while :; do
      sleep "${keep_alive_interval}"
      if command -v curl >/dev/null 2>&1; then
        curl -fsS --max-time 20 "${KEEP_ALIVE_URL}/api/status" >/dev/null 2>&1 || true
      elif [ -x /opt/hermes/.venv/bin/python ]; then
        /opt/hermes/.venv/bin/python - "${KEEP_ALIVE_URL}" <<'PYEOF' >/dev/null 2>&1 || true
import sys, urllib.request
urllib.request.urlopen(sys.argv[1].rstrip("/") + "/api/status", timeout=20).read()
PYEOF
      fi
    done
  ) &
  echo "[render-tools] keep-alive: requesting /api/status every ${keep_alive_interval}s" \
      "so the Free service does not spin down (HERMES_KEEP_ALIVE=0 to disable)"
fi

# Hand off to the upstream entrypoint -- as a supervised child rather than an
# exec. The upstream script handles privilege drop, dashboard backgrounding,
# and the gateway exec.
#
# The reason this wrapper must outlive the entrypoint by a few seconds is the
# shutdown path. tini was started with -g, so a container stop signals the
# whole process group: the gateway exits almost immediately, tini sees its
# direct child (this script, in the old exec design) exit, and the container
# is torn down -- SIGKILLing the sync daemon mid-final-push. Every restart
# then silently dropped whatever had not been pushed in the last ~30s.
#
# Crash recovery used to be "exit with the gateway's status and let Render
# restart the container". On the Free tier that costs a full boot cycle
# (state restore + skills sync + dashboard start, 1-2 minutes) during which
# the chat tab and Telegram are dead -- observed in production as restarts
# every ~3 minutes. Instead, an *unexpected* gateway exit is now restarted
# in place, bounded by HERMES_ENTRYPOINT_RESTARTS (default 5) with a small
# backoff:
#   - when the dashboard side-process is still alive, only the gateway is
#     restarted (the dashboard keeps the port bound, so the web chat only
#     sees the gateway blip);
#   - when the dashboard is gone too, the whole upstream entrypoint is
#     re-run (it re-seeds config, re-syncs skills, starts a new dashboard).
# Normal exits (clean stop, TERM/INT) and an exhausted restart budget still
# fall through to the original path: flush the sync daemon's final push,
# then exit with the child's status so Render restarts the service.
UPSTREAM_ENTRYPOINT="${HERMES_UPSTREAM_ENTRYPOINT:-/opt/hermes/docker/entrypoint.sh}"
HERMES_BIN="${HERMES_BIN:-/opt/hermes/.venv/bin/hermes}"

# /proc scan for live processes whose command line contains a needle.
# Pure POSIX (no ps/pkill dependency): the dashboard and gateway argv both
# literally contain "hermes dashboard" / "hermes gateway" at the pinned tag.
proc_pids_matching() {
  proc_needle="$1"
  for proc_dir in /proc/[0-9]*; do
    [ -d "${proc_dir}" ] || continue
    proc_pid=${proc_dir#/proc/}
    [ "${proc_pid}" != "$$" ] || continue
    proc_cmd=$(tr "\000" " " < "${proc_dir}/cmdline" 2>/dev/null) || continue
    case "${proc_cmd}" in
      *"${proc_needle}"*) echo "${proc_pid}" ;;
    esac
  done
}

# One-shot memory context, logged on every gateway exit and (throttled)
# whenever the box is close to its floor. This is what makes the next
# "it crashed again" log actionable: a 137 next to "MemAvailable=9MB" is an
# OOM kill, not a Hermes bug.
memwatch_top_rss() {
  for proc_dir in /proc/[0-9]*; do
    [ -d "${proc_dir}" ] || continue
    proc_pid=${proc_dir#/proc/}
    proc_rss=$(awk "/^VmRSS:/{print \$2; exit}" "${proc_dir}/status" 2>/dev/null)
    case "${proc_rss}" in ""|*[!0-9]*) continue ;; esac
    proc_name=$(awk "/^Name:/{print \$2; exit}" "${proc_dir}/status" 2>/dev/null)
    echo "${proc_rss} ${proc_name:-?}(${proc_pid})"
  done | sort -rn | head -n 3 | while read -r rss_rss rss_entry; do
    echo "[render-tools]   ${rss_entry}: $((rss_rss / 1024))MB RSS"
  done
}

memwatch_snapshot() {
  mem_avail=$(awk "/^MemAvailable:/{printf \"%d\", \$2 / 1024; exit}" /proc/meminfo 2>/dev/null)
  echo "[render-tools] memory at exit: MemAvailable=${mem_avail:-?}MB; top RSS:" >&2
  memwatch_top_rss >&2
}

# Dashboard + memory health loop. The upstream entrypoint backgrounds the
# dashboard and never watches it: if the dashboard process dies (the classic
# 512MB OOM victim -- it hosts every web-chat agent), the gateway keeps
# running but port 10000 goes dark and Render eventually recycles the
# service. This loop notices within ~10s and restarts the dashboard with the
# exact upstream argv (including the "[dashboard]" log prefix), bounded and
# with backoff, and resets its budget once it has stayed up for a while.
# It also logs throttled memory-pressure lines (HERMES_MEMWATCH=0 disables).
dashboard_enabled() {
  case "${HERMES_DASHBOARD:-}" in
    1|true|TRUE|True|yes|YES|Yes) return 0 ;;
    *) return 1 ;;
  esac
}

start_dashboard_supervised() {
  dash_host="${HERMES_DASHBOARD_HOST:-0.0.0.0}"
  dash_port="${HERMES_DASHBOARD_PORT:-9119}"
  dash_args="dashboard --host ${dash_host} --port ${dash_port} --no-open"
  case "${dash_host}" in
    127.0.0.1|localhost) ;;
    *) dash_args="${dash_args} --insecure" ;;
  esac
  echo "[render-tools] starting dashboard on ${dash_host}:${dash_port} (supervised)" >&2
  if command -v stdbuf >/dev/null 2>&1; then
    ( stdbuf -oL -eL gosu hermes "${HERMES_BIN}" ${dash_args} 2>&1 \
      | sed -u "s/^/[dashboard] /" ) &
  else
    ( gosu hermes "${HERMES_BIN}" ${dash_args} 2>&1 \
      | sed "s/^/[dashboard] /" ) &
  fi
}

if dashboard_enabled; then
  (
    hw_boot=$(date +%s)
    hw_attempts=0
    hw_max="${HERMES_DASHBOARD_RESTARTS:-10}"
    hw_last_start=0
    hw_gave_up=0
    hw_mem_last=0
    hw_interval="${HERMES_HEALTH_INTERVAL_SECONDS:-10}"
    case "${hw_interval}" in ""|*[!0-9]*) hw_interval=10 ;; esac
    hw_thresh="${HERMES_MEMWATCH_MB:-100}"
    case "${hw_thresh}" in ""|*[!0-9]*) hw_thresh=100 ;; esac
    hw_grace="${HERMES_HEALTH_GRACE_SECONDS:-60}"
    case "${hw_grace}" in ""|*[!0-9]*) hw_grace=60 ;; esac
    while :; do
      sleep "${hw_interval}"
      # -- memory telemetry (throttled to one line per 2 minutes) --
      if [ "${HERMES_MEMWATCH:-1}" = "1" ] && [ -r /proc/meminfo ]; then
        hw_avail=$(awk "/^MemAvailable:/{print int(\$2 / 1024); exit}" /proc/meminfo 2>/dev/null)
        case "${hw_avail}" in
          ""|*[!0-9]*) ;;
          *)
            if [ "${hw_avail}" -lt "${hw_thresh}" ] \
               && [ $(( $(date +%s) - hw_mem_last )) -ge 120 ]; then
              hw_mem_last=$(date +%s)
              echo "[render-tools] memory pressure: MemAvailable=${hw_avail}MB (floor ${hw_thresh}MB)" >&2
              memwatch_top_rss >&2
            fi
            ;;
        esac
      fi
      # -- dashboard watchdog (skips the grace window: the entrypoint is
      #    still seeding/syncing before it starts the dashboard) --
      [ $(( $(date +%s) - hw_boot )) -ge "${hw_grace}" ] || continue
      if [ -n "$(proc_pids_matching "hermes dashboard")" ]; then
        # it is up: once it has survived 5 minutes, forgive earlier crashes
        if [ $(( $(date +%s) - hw_last_start )) -ge 300 ]; then
          hw_attempts=0
          hw_gave_up=0
        fi
        continue
      fi
      [ "${hw_gave_up}" -eq 0 ] || continue
      hw_attempts=$((hw_attempts + 1))
      if [ "${hw_attempts}" -gt "${hw_max}" ]; then
        echo "[render-tools] warning: dashboard keeps exiting; leaving it down (Telegram/gateway unaffected)." >&2
        echo "[render-tools] warning: a container restart retries it: HERMES_DASHBOARD_RESTARTS=${hw_max} exceeded." >&2
        hw_gave_up=1
        continue
      fi
      echo "[render-tools] warning: dashboard process not found; restarting (${hw_attempts}/${hw_max})" >&2
      hw_last_start=$(date +%s)
      start_dashboard_supervised
    done
  ) &
fi

stop_requested=0
on_stop() { stop_requested=1; }
trap on_stop TERM INT

restarts_max="${HERMES_ENTRYPOINT_RESTARTS:-5}"
case "${restarts_max}" in ""|*[!0-9]*) restarts_max=5 ;; esac
restarts_used=0
# In-place "gateway only" restarts are only valid when the container command
# really is the gateway (the image default). Any other CMD goes through the
# full upstream entrypoint instead.
is_gateway_cmd=0
if [ "${1:-}" = "gateway" ] && [ "${2:-}" = "run" ]; then
  is_gateway_cmd=1
fi

child_status=0

while :; do
  if [ "${restarts_used}" -eq 0 ]; then
    "${UPSTREAM_ENTRYPOINT}" "$@" &
    CHILD=$!
  elif [ "${is_gateway_cmd}" -eq 1 ] \
       && [ -n "$(proc_pids_matching "hermes dashboard")" ]; then
    echo "[render-tools] dashboard still up; restarting gateway only (${restarts_used}/${restarts_max})" >&2
    gosu hermes "${HERMES_BIN}" gateway run &
    CHILD=$!
  else
    echo "[render-tools] restarting upstream entrypoint (${restarts_used}/${restarts_max})" >&2
    "${UPSTREAM_ENTRYPOINT}" "$@" &
    CHILD=$!
  fi
  child_started=$(date +%s)

  set +e
  wait "${CHILD}"
  child_status=$?
  # A trapped signal interrupts `wait` without reaping the child; when that
  # happened, wait again for the child's real exit status (this returns
  # immediately if the child exited in the meantime).
  if kill -0 "${CHILD}" 2>/dev/null; then
    wait "${CHILD}"
    child_status=$?
  fi

  child_uptime=$(( $(date +%s) - child_started ))
  echo "[render-tools] gateway exited (status ${child_status}, uptime ${child_uptime}s)" >&2
  if [ "${child_status}" -eq 137 ]; then
    echo "[render-tools] status 137 = SIGKILL, which on this image is almost always the kernel OOM killer." >&2
    echo "[render-tools] lower HERMES_AGENT_CACHE_MAX_SIZE / fewer concurrent chats, or upgrade the instance." >&2
  fi
  memwatch_snapshot

  # Clean stop (container shutdown, failover handoff) or a clean exit:
  # no restart, fall through to the flush path.
  if [ "${stop_requested}" -eq 1 ]; then
    break
  fi
  case "${child_status}" in
    0|130|143) break ;;
  esac

  if [ "${restarts_used}" -ge "${restarts_max}" ]; then
    echo "[render-tools] warning: gateway keeps exiting; giving up so Render restarts the container for a full boot" >&2
    break
  fi
  restarts_used=$((restarts_used + 1))
  restart_backoff=$((restarts_used * 5))
  [ "${restart_backoff}" -gt 30 ] && restart_backoff=30
  echo "[render-tools] unexpected exit; restarting in ${restart_backoff}s" >&2
  restart_left="${restart_backoff}"
  while [ "${restart_left}" -gt 0 ] && [ "${stop_requested}" -eq 0 ]; do
    sleep 1
    restart_left=$((restart_left - 1))
  done
  [ "${stop_requested}" -eq 0 ] || break
  # Belt and braces: a straggler gateway from the dead run would fight the
  # new one for platform polling (the exact "terminated by other
  # getUpdates request" symptom when two pollers share a token).
  for straggler in $(proc_pids_matching "hermes gateway"); do
    kill -KILL "${straggler}" 2>/dev/null || true
  done
done

# The gateway is gone. Make sure the sync daemon is stopping too -- a
# failover role change kills only the gateway, and a daemon left running
# would keep this container alive forever -- then give its final push the
# rest of the flush window before letting tini tear the container down.
if [ "${GIT_BACKEND}" -eq 1 ]; then
  daemon_pid="$(cat "${GIT_DAEMON_PIDFILE}" 2>/dev/null || true)"
  if [ -n "${daemon_pid}" ]; then
    kill -TERM "${daemon_pid}" 2>/dev/null || true
  fi
  kill -TERM "${GIT_DAEMON_SUPERVISOR}" 2>/dev/null || true
  flush_wait="${HERMES_SHUTDOWN_FLUSH_SECONDS:-20}"
  case "${flush_wait}" in
    ''|*[!0-9]*) flush_wait=20 ;;
  esac
  if [ -n "${daemon_pid}" ] && [ "${flush_wait}" -gt 0 ]; then
    flush_deadline=$(( $(date +%s) + flush_wait ))
    while kill -0 "${daemon_pid}" 2>/dev/null \
          && [ "$(date +%s)" -lt "${flush_deadline}" ]; do
      sleep 1
    done
    kill -KILL "${daemon_pid}" 2>/dev/null || true
  fi
  wait "${GIT_DAEMON_SUPERVISOR}" 2>/dev/null || true
fi

exit "${child_status}"
