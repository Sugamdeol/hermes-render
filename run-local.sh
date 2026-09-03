#!/usr/bin/env bash
#
# run-local.sh — run this Hermes-on-Render agent on your own machine.
#
# One self-contained file. It builds the same image the Render Blueprint
# builds (same Dockerfile, same pinned upstream Hermes tag, same
# render-oss/skills commit, same overlay skill, same dashboard plugin,
# same boot patcher) and runs it with the same environment configuration
# that render.yaml declares — minus the parts that only make sense on
# Render (Free-tier ephemerality, sync:false prompts).
#
# It works in two modes:
#
#   * In-repo   — sitting next to Dockerfile/, scripts/, skills/,
#                 dashboard-plugins/, it builds straight from those files.
#   * Standalone— if those files are missing (you copied just this script
#                 somewhere), it unpacks an embedded copy of them baked
#                 into the bottom of this file and builds from that.
#
# Quick start:
#
#   ./run-local.sh                      # build + start, dashboard on :10000
#   ./run-local.sh logs -f              # follow gateway logs
#   ./run-local.sh shell                # root shell in the container
#   ./run-local.sh cli                  # interactive `hermes` CLI session
#   ./run-local.sh down                 # stop and remove the container
#
# Secrets: put them in a local .env (see .env.example) or export them
# before running. Nothing is written back to the repo.
#
# Docs: README.md in this repo, and
# https://github.com/NousResearch/hermes-agent
#
set -euo pipefail

# --------------------------------------------------------------------------
# Defaults (mirroring render.yaml)
# --------------------------------------------------------------------------

SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
SELF_DIR="$(dirname "$SELF")"

# The marker is split so the script can grep for it without matching itself.
EMBED_MARK="__EMBEDDED_CONTEXT""_BEGIN__"

CONTAINER_NAME="${HERMES_LOCAL_NAME:-hermes-render-local}"
IMAGE_TAG="${HERMES_LOCAL_IMAGE:-hermes-render-local:latest}"
VOLUME_NAME="${HERMES_LOCAL_VOLUME:-hermes-render-local-data}"
DATA_DIR="${HERMES_LOCAL_DATA_DIR:-}"          # empty => use the named volume
HOST_PORT="${HERMES_LOCAL_PORT:-10000}"
BIND_ADDR="${HERMES_LOCAL_BIND:-127.0.0.1}"    # dashboard has no auth: loopback by default
ENV_FILE="${HERMES_LOCAL_ENV_FILE:-$SELF_DIR/.env}"
ENGINE="${HERMES_LOCAL_ENGINE:-}"
STATE_DIR="${HERMES_LOCAL_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/hermes-render-local}"
CACHE_DIR="${HERMES_LOCAL_CACHE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/hermes-render-local}"

# Container-internal port. Kept at 10000 so the config matches Render exactly;
# only the host-side mapping changes.
CONTAINER_PORT=10000

# Build args also pinned in the Dockerfile. Override to bump versions without
# editing the Dockerfile (e.g. HERMES_IMAGE=...:v2026.6.0 ./run-local.sh build).
HERMES_IMAGE_ARG="${HERMES_IMAGE:-}"
RENDER_SKILLS_REPO_ARG="${RENDER_SKILLS_REPO:-}"
RENDER_SKILLS_REF_ARG="${RENDER_SKILLS_REF:-}"

SOPS_VERSION="${SOPS_VERSION:-v3.13.3}"
AGE_VERSION="${AGE_VERSION:-v1.3.1}"
SECRETS_FORCE="${RENDER_TOOLS_SECRETS_FORCE:-0}"

# Failover: when this local instance shares a state repo with the Render
# service, the higher priority here makes Render stand down while we run, and
# resume when we stop. Off unless --takeover is passed.
TAKEOVER=0
LOCAL_PRIORITY="${HERMES_INSTANCE_PRIORITY:-100}"

REBUILD=0
NO_CACHE=0
PULL=0
DETACH=1
FREE_LIMITS=0
ENABLE_TUI=""
DRY_RUN=0
FOLLOW_LOGS=0
ISOLATE=0

# Variables that a second, simultaneously running instance (e.g. the Render
# service) cannot safely share with this one. A chat-platform token can only be
# consumed by one long-poller/socket at a time, and the state repo is a
# last-writer-wins snapshot of the whole /opt/data tree.
CHANNEL_VARS=(
  TELEGRAM_BOT_TOKEN DISCORD_BOT_TOKEN SLACK_BOT_TOKEN SLACK_APP_TOKEN
  SIGNAL_PHONE_NUMBER EMAIL_ADDRESS EMAIL_PASSWORD EMAIL_IMAP_HOST
  EMAIL_SMTP_HOST
)
STATE_SYNC_VARS=(GIT_STATE_REPO GIT_STATE_TOKEN)

# Secret-ish / user-supplied variables forwarded from your shell or .env.
# Anything set in the env file is forwarded regardless; this list is what we
# additionally pick up from an already-exported shell environment.
PASSTHROUGH_VARS=(
  # LLM providers
  OPENROUTER_API_KEY ANTHROPIC_API_KEY OPENAI_API_KEY GOOGLE_API_KEY
  GEMINI_API_KEY HF_TOKEN GLM_API_KEY KIMI_API_KEY DEEPSEEK_API_KEY
  MISTRAL_API_KEY GROQ_API_KEY XAI_API_KEY TOGETHER_API_KEY FIREWORKS_API_KEY
  CEREBRAS_API_KEY NOUS_API_KEY OLLAMA_API_KEY OLLAMA_HOST BYNARA_API_KEY
  # Render MCP
  RENDER_MCP_API_KEY
  # Chat platforms
  TELEGRAM_BOT_TOKEN TELEGRAM_ALLOWED_USERS DISCORD_BOT_TOKEN
  SLACK_BOT_TOKEN SLACK_APP_TOKEN SIGNAL_PHONE_NUMBER
  EMAIL_ADDRESS EMAIL_PASSWORD EMAIL_IMAP_HOST EMAIL_SMTP_HOST
  # Tools
  BROWSERBASE_API_KEY BROWSERBASE_PROJECT_ID EXA_API_KEY FIRECRAWL_API_KEY
  TAVILY_API_KEY SERPER_API_KEY HONCHO_API_KEY GITHUB_TOKEN E2B_API_KEY
  # OpenAI-compatible API server
  API_SERVER_ENABLED API_SERVER_KEY API_SERVER_HOST API_SERVER_PORT
  API_SERVER_CORS_ORIGINS
  # Optional git state backend (a private GitHub repo). GITHUB_TOKEN is
  # already forwarded above and is used when GIT_STATE_TOKEN is unset.
  GIT_STATE_REPO GIT_STATE_TOKEN GIT_STATE_BRANCH GIT_STATE_AGE_RECIPIENT
  GIT_STATE_WATCH GIT_STATE_WATCH_SECONDS GIT_STATE_DEBOUNCE_SECONDS
  GIT_STATE_MIN_PUSH_INTERVAL_SECONDS GIT_STATE_SEED_FORCE
  GIT_STATE_ENV_MODE
  # Wrapper-level tunables. (The keep-alive is a no-op locally: it only runs
  # when RENDER_EXTERNAL_URL is set, which is a Render-injected variable.)
  HERMES_KEEP_ALIVE HERMES_KEEP_ALIVE_SECONDS HERMES_SHUTDOWN_FLUSH_SECONDS
  HERMES_RESTORE_ATTEMPTS HERMES_RESTORE_TIMEOUT_SECONDS
)

# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

log()  { printf '\033[1;36m[run-local]\033[0m %s\n' "$*" >&2; }
warn() { printf '\033[1;33m[run-local]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[run-local]\033[0m %s\n' "$*" >&2; exit 1; }

run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    { printf '+ '; printf '%q ' "$@"; printf '\n'; } >&2
    return 0
  fi
  "$@"
}

usage() {
  cat <<'EOF'
run-local.sh — run the Hermes-on-Render agent locally with the same
configuration, skills, plugin, and boot patching as the Render Blueprint.

USAGE
  ./run-local.sh [command] [options]

COMMANDS
  up (default)     Build the image if needed, then start the container.
  build            Build (or rebuild) the image only.
  down             Stop and remove the container. The data volume is kept.
  restart          Recreate the container from the current image + env.
  logs [-f]        Show container logs.
  status           Container state, health probe, and effective settings.
  shell            Open a root shell inside the running container.
  cli [args...]    Run the interactive `hermes` CLI inside the container
                   (defaults to `hermes chat`).
  config           Print /opt/data/config.yaml as the agent sees it.
  print-env        Print the exact environment the container gets.
  materialize DIR  Write the build context (Dockerfile, scripts, skills,
                   dashboard-plugins) to DIR and exit.
  purge            Remove the container AND the persistent data volume.
  secrets SUB      Manage the repo's encrypted secrets (SOPS + age):
                     init    generate an age key and record the recipient
                     edit    open the decrypted secrets in $EDITOR
                     show    list variable names with redacted values
                     status  where the key, config, and secrets file are
  update-embed     Re-pack the embedded standalone copy from the repo files
                   next to this script (maintainer command).
  help             This message.

OPTIONS
  -p, --port N         Host port for the dashboard (default 10000).
      --bind ADDR      Host bind address (default 127.0.0.1; the dashboard
                       has no authentication — 0.0.0.0 exposes it to your LAN).
      --data-dir PATH  Bind-mount PATH at /opt/data instead of using the
                       named Docker volume.
      --env-file PATH  Env file with secrets (default ./.env if present).
      --name NAME      Container name (default hermes-render-local).
      --image TAG      Image tag to build/run (default hermes-render-local:latest).
      --rebuild        Force an image rebuild before starting.
      --no-cache       Build without the layer cache (implies --rebuild).
      --pull           Pull a newer base image while building.
      --tui / --no-tui Toggle the dashboard's in-browser Chat tab
                       (HERMES_DASHBOARD_TUI; on by default -- the chat
                       plugin needs it; --no-tui frees a little headroom).
      --free-limits    Constrain the container to Render Free-ish resources
                       (512MB RAM, 0.5 CPU) to reproduce that environment.
      --takeover       Take over messaging from the Render service while this
                       instance runs: Render goes quiet, and resumes after
                       this one stops and uploads its final state. Requires a
                       shared GIT_STATE_REPO. The opposite of --isolate.
      --isolate        Do not forward chat-platform tokens or the state
                       repo, so this instance cannot collide with a Render
                       service running the same bot/backup. Chat via
                       the dashboard or `./run-local.sh cli` instead.
      --foreground     Run in the foreground instead of detaching.
      --engine NAME    docker | podman (auto-detected).
      --dry-run        Print the commands instead of running them.

ENVIRONMENT
  Secrets come from --env-file and from your exported shell environment
  (OPENROUTER_API_KEY, ANTHROPIC_API_KEY, BYNARA_API_KEY, RENDER_MCP_API_KEY,
  TELEGRAM_BOT_TOKEN, ...). Build pins can be overridden with HERMES_IMAGE,
  RENDER_SKILLS_REPO, RENDER_SKILLS_REF.

  Unlike Render Free, /opt/data here is persistent (named volume by default),
  so config.yaml, sessions, memories, and skills survive restarts.

EXAMPLES
  OPENROUTER_API_KEY=sk-... ./run-local.sh
  ./run-local.sh --port 8080 --tui
  ./run-local.sh --data-dir ./hermes-data --free-limits
  ./run-local.sh cli chat
EOF
}

# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------

COMMAND=""
EXTRA_ARGS=()

while [ $# -gt 0 ]; do
  case "$1" in
    up|build|down|stop|restart|logs|status|shell|cli|config|print-env|materialize|purge|update-embed|secrets|help)
      if [ -z "$COMMAND" ]; then
        COMMAND="$1"
      else
        EXTRA_ARGS+=("$1")
      fi
      shift
      ;;
    -p|--port)        HOST_PORT="${2:?--port needs a value}"; shift 2 ;;
    --bind)           BIND_ADDR="${2:?--bind needs a value}"; shift 2 ;;
    --data-dir)       DATA_DIR="${2:?--data-dir needs a value}"; shift 2 ;;
    --env-file)       ENV_FILE="${2:?--env-file needs a value}"; shift 2 ;;
    --name)           CONTAINER_NAME="${2:?--name needs a value}"; shift 2 ;;
    --image)          IMAGE_TAG="${2:?--image needs a value}"; shift 2 ;;
    --engine)         ENGINE="${2:?--engine needs a value}"; shift 2 ;;
    --rebuild)        REBUILD=1; shift ;;
    --no-cache)       NO_CACHE=1; REBUILD=1; shift ;;
    --pull)           PULL=1; shift ;;
    --tui)            ENABLE_TUI=1; shift ;;
    --no-tui)         ENABLE_TUI=0; shift ;;
    --free-limits)    FREE_LIMITS=1; shift ;;
    --isolate)        ISOLATE=1; shift ;;
    --takeover)       TAKEOVER=1; shift ;;
    --foreground|-i)  DETACH=0; shift ;;
    --dry-run)        DRY_RUN=1; shift ;;
    -f|--follow)      FOLLOW_LOGS=1; shift ;;
    -h|--help)        usage; exit 0 ;;
    --)               shift; EXTRA_ARGS+=("$@"); break ;;
    *)                EXTRA_ARGS+=("$1"); shift ;;
  esac
done

COMMAND="${COMMAND:-up}"
[ "$COMMAND" = "stop" ] && COMMAND="down"

if [ "$COMMAND" = "help" ]; then usage; exit 0; fi

# --------------------------------------------------------------------------
# Container engine
# --------------------------------------------------------------------------

detect_engine() {
  if [ -n "$ENGINE" ]; then
    command -v "$ENGINE" >/dev/null 2>&1 || die "container engine '$ENGINE' not found in PATH"
    return
  fi
  if command -v docker >/dev/null 2>&1; then
    ENGINE=docker
  elif command -v podman >/dev/null 2>&1; then
    ENGINE=podman
  else
    die "neither docker nor podman found. Install Docker Desktop / Podman and retry."
  fi
}

engine_ready() {
  "$ENGINE" info >/dev/null 2>&1 || die \
    "'$ENGINE info' failed — is the daemon running? (start Docker Desktop / 'podman machine start')"
}

container_exists() { "$ENGINE" ps -a --format '{{.Names}}' | grep -Fxq "$CONTAINER_NAME"; }
container_running() { "$ENGINE" ps --format '{{.Names}}' | grep -Fxq "$CONTAINER_NAME"; }
image_exists() { "$ENGINE" image inspect "$IMAGE_TAG" >/dev/null 2>&1; }

require_running() {
  container_running || die "container '$CONTAINER_NAME' is not running. Start it with: ./run-local.sh up"
}

# --------------------------------------------------------------------------
# Build context: the repo next to this script, or the embedded copy
# --------------------------------------------------------------------------

CONTEXT_FILES=(Dockerfile scripts skills dashboard-plugins env .sops.yaml)

context_is_complete() {
  local dir="$1" f
  for f in "${CONTEXT_FILES[@]}"; do
    [ -e "$dir/$f" ] || return 1
  done
  [ -f "$dir/scripts/bootstrap.sh" ] || return 1
  [ -f "$dir/scripts/patch-config.py" ] || return 1
  [ -f "$dir/scripts/git-storage.py" ] || return 1
  [ -f "$dir/scripts/patch-model-discovery.py" ] || return 1
  [ -f "$dir/scripts/seed-env.py" ] || return 1
  return 0
}

has_embedded_payload() { grep -q "^${EMBED_MARK}\$" "$SELF"; }

extract_embedded_payload() {
  local dest="$1"
  has_embedded_payload || die \
    "this copy of run-local.sh has no embedded build context and no repo files next to it.
Clone https://github.com/Sugamdeol/hermes-render and run the script from there,
or regenerate a standalone copy with: ./run-local.sh update-embed"
  mkdir -p "$dest"
  sed -e "1,/^${EMBED_MARK}\$/d" "$SELF" | base64 -d | tar -xzf - -C "$dest"
}

# Resolves BUILD_CONTEXT.
resolve_context() {
  if context_is_complete "$SELF_DIR"; then
    BUILD_CONTEXT="$SELF_DIR"
    CONTEXT_SOURCE="repo"
    return
  fi
  local digest dest
  digest="$( (has_embedded_payload && sed -e "1,/^${EMBED_MARK}\$/d" "$SELF") | cksum | tr -d ' ' || true)"
  dest="$CACHE_DIR/context-${digest:-0}"
  if ! context_is_complete "$dest"; then
    log "no repo files next to this script — unpacking the embedded build context"
    rm -rf "$dest"
    extract_embedded_payload "$dest"
    context_is_complete "$dest" || die "embedded build context looks incomplete"
  fi
  BUILD_CONTEXT="$dest"
  CONTEXT_SOURCE="embedded ($dest)"
}


# --------------------------------------------------------------------------
# Encrypted repo secrets (SOPS + age)
# --------------------------------------------------------------------------

# Tools are cached per version under the script's cache dir so nothing has to
# be installed system-wide and CI can reuse them.
TOOLS_DIR="$CACHE_DIR/bin"

host_os() { uname -s | tr '[:upper:]' '[:lower:]'; }

host_arch() {
  case "$(uname -m)" in
    x86_64|amd64) echo amd64 ;;
    aarch64|arm64) echo arm64 ;;
    *) die "unsupported architecture $(uname -m) for sops/age downloads" ;;
  esac
}

# Verifies the download against the release's own checksums.txt. Set
# SOPS_SHA256 to pin an exact digest instead, which is stronger.
ensure_sops() {
  if command -v sops >/dev/null 2>&1; then SOPS_BIN="$(command -v sops)"; return; fi
  SOPS_BIN="$TOOLS_DIR/sops-$SOPS_VERSION"
  [ -x "$SOPS_BIN" ] && return
  local os arch base tmp expected
  os="$(host_os)"; arch="$(host_arch)"
  base="https://github.com/getsops/sops/releases/download/${SOPS_VERSION}"
  mkdir -p "$TOOLS_DIR"
  tmp="$(mktemp -d)"
  log "downloading sops ${SOPS_VERSION} (${os}/${arch}) into $TOOLS_DIR"
  curl -fsSL --retry 3 -o "$tmp/sops" "${base}/sops-${SOPS_VERSION}.${os}.${arch}" \
    || die "could not download sops. Install it manually and re-run."
  if [ -n "${SOPS_SHA256:-}" ]; then
    expected="$SOPS_SHA256"
  else
    curl -fsSL --retry 3 -o "$tmp/checksums.txt" "${base}/sops-${SOPS_VERSION}.checksums.txt" \
      || die "could not download the sops checksum list"
    expected="$(grep " sops-${SOPS_VERSION}.${os}.${arch}\$" "$tmp/checksums.txt" | head -n1 | cut -d' ' -f1)"
    [ -n "$expected" ] || die "no checksum published for sops ${os}.${arch}"
  fi
  echo "${expected}  ${tmp}/sops" | shasum -a 256 -c - >/dev/null 2>&1 \
    || echo "${expected}  ${tmp}/sops" | sha256sum -c - >/dev/null \
    || die "sops checksum mismatch — refusing to use the download"
  chmod +x "$tmp/sops"; mv "$tmp/sops" "$SOPS_BIN"; rm -rf "$tmp"
}

ensure_age_keygen() {
  if command -v age-keygen >/dev/null 2>&1; then AGE_KEYGEN_BIN="$(command -v age-keygen)"; return; fi
  AGE_KEYGEN_BIN="$TOOLS_DIR/age-keygen-$AGE_VERSION"
  [ -x "$AGE_KEYGEN_BIN" ] && return
  local os arch tmp
  os="$(host_os)"; arch="$(host_arch)"
  mkdir -p "$TOOLS_DIR"
  tmp="$(mktemp -d)"
  log "downloading age ${AGE_VERSION} (${os}/${arch}) for key generation"
  curl -fsSL --retry 3 -o "$tmp/age.tgz" \
    "https://github.com/FiloSottile/age/releases/download/${AGE_VERSION}/age-${AGE_VERSION}-${os}-${arch}.tar.gz" \
    || die "could not download age. Install it manually and re-run."
  tar -xzf "$tmp/age.tgz" -C "$tmp"
  [ -f "$tmp/age/age-keygen" ] || die "age tarball did not contain age-keygen"
  chmod +x "$tmp/age/age-keygen"; mv "$tmp/age/age-keygen" "$AGE_KEYGEN_BIN"; rm -rf "$tmp"
}

age_key_file() {
  if [ -n "${SOPS_AGE_KEY_FILE:-}" ]; then printf '%s' "$SOPS_AGE_KEY_FILE"; return; fi
  printf '%s' "${XDG_CONFIG_HOME:-$HOME/.config}/sops/age/keys.txt"
}

have_age_key() { [ -n "${SOPS_AGE_KEY:-}" ] || [ -s "$(age_key_file)" ]; }

secrets_file() { printf '%s' "${BUILD_CONTEXT:-$SELF_DIR}/env/secrets.enc.env"; }

# Decrypt the committed secrets into ENV_MAP. Silent no-op when the file or
# the key is absent, so a collaborator without the key can still run the agent
# with their own .env.
load_repo_secrets() {
  local file; file="$(secrets_file)"
  [ -f "$file" ] || return 0
  if ! have_age_key; then
    warn "env/secrets.enc.env exists but no age key was found at $(age_key_file)."
    warn "Run './run-local.sh secrets init' or set SOPS_AGE_KEY to use it."
    return 0
  fi
  ensure_sops
  local plaintext
  if ! plaintext="$("$SOPS_BIN" --decrypt --input-type dotenv --output-type dotenv "$file" 2>&1)"; then
    warn "could not decrypt env/secrets.enc.env:"
    warn "  ${plaintext%%$'\n'*}"
    return 0
  fi
  local line key value
  while IFS= read -r line; do
    case "$line" in ''|\#*) continue ;; *=*) ;; *) continue ;; esac
    key="${line%%=*}"; value="${line#*=}"
    case "$key" in ''|*[!A-Za-z0-9_]*) continue ;; esac
    [ -z "$value" ] && continue
    set_env "$key" "$value"
  done <<< "$plaintext"
}

cmd_secrets() {
  local sub="${EXTRA_ARGS[0]:-status}"
  resolve_context
  local enc; enc="$(secrets_file)"
  case "$sub" in
    init)
      ensure_age_keygen
      local keyfile recipient
      keyfile="$(age_key_file)"
      if [ -s "$keyfile" ]; then
        log "reusing the existing age key at $keyfile"
        recipient="$("$AGE_KEYGEN_BIN" -y "$keyfile")"
      else
        mkdir -p "$(dirname "$keyfile")"
        ( umask 077; "$AGE_KEYGEN_BIN" -o "$keyfile" 2>/dev/null )
        chmod 600 "$keyfile"
        recipient="$("$AGE_KEYGEN_BIN" -y "$keyfile")"
        log "generated a new age key at $keyfile"
      fi
      local sops_cfg="${BUILD_CONTEXT}/.sops.yaml"
      if [ -f "$sops_cfg" ]; then
        # Portable in-place edit: BSD and GNU sed disagree about -i.
        local tmp; tmp="$(mktemp)"
        sed "s|age: .*|age: ${recipient}|" "$sops_cfg" > "$tmp" && mv "$tmp" "$sops_cfg"
        log "recorded the public recipient in .sops.yaml"
      fi
      cat >&2 <<EOF

  age recipient (public, safe to commit):
    ${recipient}

  Next steps:
    1. ./run-local.sh secrets edit          # add your keys
    2. git add .sops.yaml env/secrets.enc.env && git commit
    3. Paste the PRIVATE key into Render → Environment → SOPS_AGE_KEY:
         cat ${keyfile}
       (the AGE-SECRET-KEY-... line; never commit it)

EOF
      ;;
    edit)
      ensure_sops
      have_age_key || die "no age key found. Run './run-local.sh secrets init' first."
      grep -q 'REPLACE_WITH_AGE_RECIPIENT' "${BUILD_CONTEXT}/.sops.yaml" 2>/dev/null \
        && die "no recipient in .sops.yaml yet. Run './run-local.sh secrets init' first."
      if [ ! -f "$enc" ]; then
        log "creating $enc"
        local tmp; tmp="$(mktemp -d)"; chmod 700 "$tmp"
        cat > "$tmp/secrets.env" <<'TEMPLATE'
# Secrets for this Hermes deployment. Values are encrypted by SOPS on save;
# the keys stay readable so diffs are reviewable. Delete what you do not use.
#
# At least one LLM provider:
OPENROUTER_API_KEY=
ANTHROPIC_API_KEY=
BYNARA_API_KEY=
# Render MCP server (config.yaml reads ${RENDER_MCP_API_KEY} at gateway start):
RENDER_MCP_API_KEY=
# Chat platforms:
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_USERS=
# Optional git state backup (private repo, e.g. you/hermes-storage).
# Needs a fine-grained PAT with Contents: read and write on that repo only:
GIT_STATE_REPO=
GIT_STATE_TOKEN=
TEMPLATE
        ( cd "$BUILD_CONTEXT" && "$SOPS_BIN" --encrypt --input-type dotenv --output-type dotenv \
            --output "$enc" "$tmp/secrets.env" ) || { rm -rf "$tmp"; die "initial encryption failed"; }
        rm -rf "$tmp"
      fi
      ( cd "$BUILD_CONTEXT" && "$SOPS_BIN" edit "$enc" )
      log "saved. Commit env/secrets.enc.env to share it."
      ;;
    show)
      ensure_sops
      have_age_key || die "no age key found. Run './run-local.sh secrets init' first."
      [ -f "$enc" ] || die "no env/secrets.enc.env yet. Run './run-local.sh secrets edit'."
      # Names and last-4 only; use `sops -d` directly if you need the values.
      "$SOPS_BIN" --decrypt --input-type dotenv --output-type dotenv "$enc" \
        | while IFS= read -r line; do
            case "$line" in ''|\#*) continue ;; *=*) ;; *) continue ;; esac
            printf '%s=%s\n' "${line%%=*}" "$(redact "${line%%=*}" "${line#*=}")"
          done
      ;;
    status|"")
      echo "sops config:  ${BUILD_CONTEXT}/.sops.yaml $( [ -f "${BUILD_CONTEXT}/.sops.yaml" ] && echo present || echo missing)"
      echo "secrets file: $enc $( [ -f "$enc" ] && echo present || echo 'not created')"
      echo "age key:      $(age_key_file) $( have_age_key && echo available || echo 'not found')"
      if [ -f "${BUILD_CONTEXT}/.sops.yaml" ]; then
        echo "recipient:    $(grep -o 'age1[a-z0-9]*' "${BUILD_CONTEXT}/.sops.yaml" | head -n1 || echo 'not set')"
      fi
      ;;
    *)
      die "unknown secrets subcommand '$sub' (use init, edit, show, or status)"
      ;;
  esac
}

# --------------------------------------------------------------------------
# Environment assembly (render.yaml parity)
# --------------------------------------------------------------------------

declare -A ENV_MAP=()
ENV_ORDER=()

set_env() {
  local key="$1" value="$2"
  if [ -z "${ENV_MAP[$key]+x}" ]; then ENV_ORDER+=("$key"); fi
  ENV_MAP["$key"]="$value"
}

unset_env() {
  local key="$1"
  if [ -n "${ENV_MAP[$key]+x}" ]; then unset 'ENV_MAP[$key]'; fi
}

gateway_token() {
  local file="$STATE_DIR/gateway-token"
  if [ -n "${HERMES_GATEWAY_TOKEN:-}" ]; then
    printf '%s' "$HERMES_GATEWAY_TOKEN"
    return
  fi
  if [ ! -s "$file" ]; then
    mkdir -p "$STATE_DIR"
    ( umask 077
      if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 32 > "$file"
      elif [ -r /dev/urandom ]; then
        LC_ALL=C tr -dc 'a-f0-9' < /dev/urandom | head -c 64 > "$file"
      else
        date +%s%N | sha256sum | cut -c1-64 > "$file"
      fi )
  fi
  tr -d '\r\n' < "$file"
}

load_env_file() {
  local file="$1" line key value
  [ -f "$file" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line#"${line%%[![:space:]]*}"}"                 # ltrim
    [ -z "$line" ] && continue
    case "$line" in \#*) continue ;; esac
    case "$line" in export\ *) line="${line#export }" ;; esac
    case "$line" in *=*) ;; *) continue ;; esac
    key="${line%%=*}"
    value="${line#*=}"
    key="${key%"${key##*[![:space:]]}"}"                    # rtrim key
    case "$key" in ''|*[!A-Za-z0-9_]*) continue ;; esac
    # Strip one layer of matching quotes, as docker --env-file would not.
    case "$value" in
      \"*\") value="${value%\"}"; value="${value#\"}" ;;
      \'*\') value="${value%\'}"; value="${value#\'}" ;;
    esac
    set_env "$key" "$value"
  done < "$file"
}

build_env() {
  # --- Blueprint-managed boot config -------------------------------------
  # env/common.env is the shared, non-secret source of truth (same values
  # render.yaml declares inline). The literals below are a fallback for a
  # standalone copy whose context has not been unpacked yet.
  if [ -f "${BUILD_CONTEXT:-$SELF_DIR}/env/common.env" ]; then
    load_env_file "${BUILD_CONTEXT:-$SELF_DIR}/env/common.env"
  fi
  set_env HERMES_HOME "/opt/data"
  set_env PORT "$CONTAINER_PORT"

  # Dashboard side-process. Bound to 0.0.0.0 inside the container so the
  # published port reaches it; the entrypoint adds --insecure for that.
  # HERMES_DASHBOARD_TUI=1 enables the in-browser Chat tab: upstream gates
  # the pure-Python chat WebSocket (/api/ws, which the bundled
  # hermes-chat-dashboard plugin drives) behind the same flag as the
  # terminal-PTY fallback. The Node/esbuild PTY only starts when /api/pty is
  # actually used, so this does not add a Node process to idle RAM.
  set_env HERMES_DASHBOARD "1"
  set_env HERMES_DASHBOARD_HOST "0.0.0.0"
  set_env HERMES_DASHBOARD_PORT "$CONTAINER_PORT"
  set_env HERMES_DASHBOARD_TUI "1"
  # No per-chat second HermesCLI interpreter (slash-command worker); the web
  # chat UI never drives the slash menu. Requires the Dockerfile
  # patch-slash-worker bake (image build).
  set_env HERMES_TUI_DISABLE_SLASH_WORKER "1"
  set_env HERMES_TUI_RPC_POOL_WORKERS "2"
  set_env HERMES_TUI_CLOSE_SESSIONS_ON_DISCONNECT "1"

  # Free-tier resource guardrails, kept for behavioural parity.
  set_env NODE_OPTIONS "--max-old-space-size=96"
  set_env MALLOC_ARENA_MAX "2"
  set_env OMP_NUM_THREADS "1"
  set_env OPENBLAS_NUM_THREADS "1"
  set_env MKL_NUM_THREADS "1"
  set_env VECLIB_MAXIMUM_THREADS "1"
  set_env NUMEXPR_NUM_THREADS "1"
  set_env TOKENIZERS_PARALLELISM "false"
  set_env MAKEFLAGS "-j1"
  set_env HERMES_AGENT_CACHE_MAX_SIZE "8"
  set_env HERMES_AGENT_CACHE_IDLE_TTL_SECONDS "600"
  # Git state-sync memory: small unchunked post buffer + capped pack memory
  # so a push cannot spike the container past 512 MB.
  set_env GIT_STATE_HTTP_POST_BUFFER_MB "64"
  set_env GIT_STATE_PACK_WINDOW_MEMORY_MB "16"
  set_env GIT_STATE_PACK_THREADS "1"

  # Git state backend defaults. Off unless GIT_STATE_REPO plus a token are
  # supplied. When it is on, every save uploads only the changed bytes, so
  # /opt/data survives even though nothing persists it locally on Render.
  set_env GIT_STATE_BRANCH "state"
  # Change-driven saves: notice a change in a few seconds, push once the tree
  # settles. The interval is only the safety net.
  set_env GIT_STATE_WATCH "1"
  set_env GIT_STATE_WATCH_SECONDS "5"
  set_env GIT_STATE_DEBOUNCE_SECONDS "10"
  set_env GIT_STATE_MIN_PUSH_INTERVAL_SECONDS "20"
  set_env GIT_STATE_INTERVAL_SECONDS "300"
  set_env GIT_STATE_MAX_COMMITS "200"

  # generateValue: true on Render — generated once and reused here.
  set_env HERMES_GATEWAY_TOKEN "$(gateway_token)"

  # --- Repo-managed encrypted secrets ------------------------------------
  # Below the env file and the shell in precedence: a local override always
  # beats the committed value, matching the container's own ordering.
  load_repo_secrets

  # --- User-supplied values ---------------------------------------------
  load_env_file "$ENV_FILE"

  local var
  for var in "${PASSTHROUGH_VARS[@]}"; do
    if [ -n "${!var:-}" ]; then set_env "$var" "${!var}"; fi
  done

  # --- Local overrides ---------------------------------------------------
  if [ "$ISOLATE" -eq 1 ]; then
    local var
    for var in "${CHANNEL_VARS[@]}" "${STATE_SYNC_VARS[@]}"; do unset_env "$var"; done
  fi
  if [ "$TAKEOVER" -eq 1 ]; then
    # The two instances need a shared meeting point to see each other; the
    # state repo provides one through its lease refs.
    if [ -z "${ENV_MAP[GIT_STATE_REPO]:-}" ]; then
      die "--takeover needs a shared state backend so the two instances can see
each other: set GIT_STATE_REPO (plus GIT_STATE_TOKEN or GITHUB_TOKEN).
Add it to your .env or export it."
    fi
    set_env HERMES_FAILOVER "1"
    set_env HERMES_INSTANCE_PRIORITY "$LOCAL_PRIORITY"
    [ -n "${ENV_MAP[HERMES_INSTANCE_ID]:-}" ] || set_env HERMES_INSTANCE_ID "local-$(hostname -s 2>/dev/null || echo laptop)"
  fi
  if [ -n "$ENABLE_TUI" ]; then set_env HERMES_DASHBOARD_TUI "$ENABLE_TUI"; fi

  # Keep the container's idea of its port aligned with the mapping.
  set_env PORT "$CONTAINER_PORT"
  set_env HERMES_DASHBOARD_PORT "$CONTAINER_PORT"
}

write_env_file() {
  local out="$1" key
  ( umask 077; : > "$out" )
  for key in "${ENV_ORDER[@]}"; do
    [ -n "${ENV_MAP[$key]+x}" ] || continue
    printf '%s=%s\n' "$key" "${ENV_MAP[$key]}" >> "$out"
  done
}

redact() {
  local key="$1" value="$2"
  case "$key" in
    *KEY|*TOKEN|*SECRET|*PASSWORD|*_KEY_*)
      if [ -n "$value" ]; then printf '%s' "****${value: -4}"; fi ;;
    *) printf '%s' "$value" ;;
  esac
}

# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

cmd_build() {
  resolve_context
  log "build context: $CONTEXT_SOURCE"
  local args=(build -t "$IMAGE_TAG" -f "$BUILD_CONTEXT/Dockerfile")
  [ "$NO_CACHE" -eq 1 ] && args+=(--no-cache)
  [ "$PULL" -eq 1 ] && args+=(--pull)
  [ -n "$HERMES_IMAGE_ARG" ] && args+=(--build-arg "HERMES_IMAGE=$HERMES_IMAGE_ARG")
  [ -n "$RENDER_SKILLS_REPO_ARG" ] && args+=(--build-arg "RENDER_SKILLS_REPO=$RENDER_SKILLS_REPO_ARG")
  [ -n "$RENDER_SKILLS_REF_ARG" ] && args+=(--build-arg "RENDER_SKILLS_REF=$RENDER_SKILLS_REF_ARG")
  args+=("$BUILD_CONTEXT")
  log "building $IMAGE_TAG (first build pulls the upstream Hermes image; expect several minutes)"
  run "$ENGINE" "${args[@]}"
}

cmd_up() {
  if [ "$REBUILD" -eq 1 ] || ! image_exists; then
    cmd_build
  else
    log "reusing existing image $IMAGE_TAG (use --rebuild to rebuild)"
  fi

  if container_exists; then
    log "removing previous container '$CONTAINER_NAME'"
    run "$ENGINE" rm -f "$CONTAINER_NAME" >/dev/null
  fi

  build_env
  local envfile="$STATE_DIR/env.$$"
  mkdir -p "$STATE_DIR"
  rm -f "$STATE_DIR"/env.* 2>/dev/null || true
  write_env_file "$envfile"
  # Expanded now, not at trap time: $envfile is function-local.
  # shellcheck disable=SC2064
  trap "rm -f '$envfile'" EXIT

  local args=(run --name "$CONTAINER_NAME" --hostname hermes-render-local)
  if [ "$DETACH" -eq 1 ]; then
    args+=(-d --restart unless-stopped)
  else
    args+=(--rm -it)
  fi
  args+=(-p "${BIND_ADDR}:${HOST_PORT}:${CONTAINER_PORT}")
  args+=(--env-file "$envfile")
  # Long enough for the final state upload before SIGKILL.
  args+=(--stop-timeout 60)

  if [ -n "$DATA_DIR" ]; then
    mkdir -p "$DATA_DIR"
    DATA_DIR="$(cd "$DATA_DIR" && pwd)"
    log "state directory (bind mount): $DATA_DIR -> /opt/data"
    args+=(-v "${DATA_DIR}:/opt/data")
  else
    log "state volume: $VOLUME_NAME -> /opt/data (persistent; 'purge' deletes it)"
    args+=(-v "${VOLUME_NAME}:/opt/data")
  fi

  if [ "$FREE_LIMITS" -eq 1 ]; then
    log "applying Render Free-ish limits: 512MB RAM / 0.5 CPU"
    args+=(--memory 512m --memory-swap 512m --cpus 0.5)
  fi

  args+=("$IMAGE_TAG")
  # Same CMD as the image / Render service.
  args+=(gateway run)

  local has_provider=0 v
  for v in OPENROUTER_API_KEY ANTHROPIC_API_KEY OPENAI_API_KEY GOOGLE_API_KEY \
           BYNARA_API_KEY HF_TOKEN GLM_API_KEY KIMI_API_KEY NOUS_API_KEY; do
    [ -n "${ENV_MAP[$v]:-}" ] && has_provider=1
  done
  if [ "$has_provider" -eq 0 ]; then
    warn "no LLM provider key found in $ENV_FILE or your shell environment."
    warn "the agent will boot, but you must add a key (dashboard API Keys tab, or an .env) before it can think."
  fi
  if [ -z "${ENV_MAP[RENDER_MCP_API_KEY]:-}" ]; then
    warn "RENDER_MCP_API_KEY is unset — the pre-registered Render MCP server will fail to authenticate."
  fi
  warn_shared_identity

  run "$ENGINE" "${args[@]}" >/dev/null
  [ "$DRY_RUN" -eq 1 ] && return 0
  [ "$DETACH" -eq 1 ] || return 0

  rm -f "$envfile"; trap - EXIT

  local url="http://${BIND_ADDR}:${HOST_PORT}"
  [ "$BIND_ADDR" = "0.0.0.0" ] && url="http://localhost:${HOST_PORT}"
  log "container started; waiting for the dashboard healthcheck (/api/status)"
  wait_for_health "$url" 180 || {
    warn "dashboard did not answer in time. Recent logs:"
    "$ENGINE" logs --tail 40 "$CONTAINER_NAME" >&2 || true
    warn "keep watching with: ./run-local.sh logs -f"
    return 0
  }
  cat >&2 <<EOF

  Hermes is up.

    Dashboard   ${url}
    Health      ${url}/api/status
    Logs        ./run-local.sh logs -f
    CLI         ./run-local.sh cli
    Stop        ./run-local.sh down

  The dashboard has no authentication; it is bound to ${BIND_ADDR}.
EOF
}

# One bot token / one state backend cannot be shared with a second live
# instance. Say so loudly before the container starts rather than after a
# duplicated reply or a clobbered backup.
warn_shared_identity() {
  local var shared=() sync=()
  for var in "${CHANNEL_VARS[@]}"; do
    [ -n "${ENV_MAP[$var]:-}" ] && shared+=("$var")
  done
  for var in "${STATE_SYNC_VARS[@]}"; do
    [ -n "${ENV_MAP[$var]:-}" ] && sync+=("$var")
  done
  [ "${#shared[@]}" -eq 0 ] && [ "${#sync[@]}" -eq 0 ] && return 0

  if [ "$TAKEOVER" -eq 1 ]; then
    log "failover enabled (priority $LOCAL_PRIORITY): the Render service will"
    log "stand down while this instance runs, and resume when it stops."
    return 0
  fi

  warn "this instance claims a shared identity:"
  [ "${#shared[@]}" -gt 0 ] && warn "  chat platforms : ${shared[*]}"
  [ "${#sync[@]}"   -gt 0 ] && warn "  state sync     : ${sync[*]}"
  warn "If the Render service is also running with the same values:"
  [ "${#shared[@]}" -gt 0 ] && warn "  - incoming messages are split or duplicated between the two agents"
  [ "${#sync[@]}"   -gt 0 ] && warn "  - each state sync overwrites the other's /opt/data snapshot"
  warn "Use --takeover to coordinate instead (Render stands down while you run),"
  warn "--isolate to drop these, or a separate bot token locally."
  return 0
}

wait_for_health() {
  local url="$1" deadline=$(( SECONDS + ${2:-120} ))
  command -v curl >/dev/null 2>&1 || { sleep 5; return 0; }
  while [ "$SECONDS" -lt "$deadline" ]; do
    container_running || return 1
    if curl -fsS --max-time 3 "${url}/api/status" >/dev/null 2>&1; then
      return 0
    fi
    sleep 3
  done
  return 1
}

cmd_down() {
  if container_exists; then
    if container_running; then
      # SIGTERM first, and wait: bootstrap holds the container open after the
      # gateway exits so the sync daemon can land its final delta push (and
      # release its failover lease), which is what lets Render resume with
      # complete state. `rm -f` would kill that mid-flight.
      log "stopping '$CONTAINER_NAME' (flushing final state backup, up to 60s)"
      run "$ENGINE" stop -t 60 "$CONTAINER_NAME" >/dev/null || true
    fi
    log "removing '$CONTAINER_NAME'"
    run "$ENGINE" rm -f "$CONTAINER_NAME" >/dev/null
  else
    log "no container named '$CONTAINER_NAME'"
  fi
}

cmd_purge() {
  cmd_down
  if [ -z "$DATA_DIR" ]; then
    log "removing data volume '$VOLUME_NAME'"
    run "$ENGINE" volume rm "$VOLUME_NAME" >/dev/null 2>&1 || true
  else
    warn "bind-mounted data dir left untouched: $DATA_DIR"
  fi
}

cmd_logs() {
  container_exists || die "no container named '$CONTAINER_NAME'"
  local args=(logs)
  { [ "$FOLLOW_LOGS" -eq 1 ] || [[ " ${EXTRA_ARGS[*]:-} " == *" -f "* ]]; } && args+=(-f)
  args+=(--tail 200 "$CONTAINER_NAME")
  exec "$ENGINE" "${args[@]}"
}

cmd_status() {
  build_env
  local url="http://${BIND_ADDR}:${HOST_PORT}"
  [ "$BIND_ADDR" = "0.0.0.0" ] && url="http://localhost:${HOST_PORT}"
  echo "engine:      $ENGINE"
  echo "image:       $IMAGE_TAG ($(image_exists && echo built || echo 'not built'))"
  echo "container:   $CONTAINER_NAME ($(container_running && echo running || { container_exists && echo stopped || echo absent; }))"
  echo "dashboard:   $url"
  echo "state:       ${DATA_DIR:-volume:$VOLUME_NAME} -> /opt/data"
  echo "env file:    $ENV_FILE $( [ -f "$ENV_FILE" ] && echo '(loaded)' || echo '(absent)')"
  echo "tui:         ${ENV_MAP[HERMES_DASHBOARD_TUI]}"
  if container_running; then
    echo
    "$ENGINE" ps --filter "name=^${CONTAINER_NAME}$" \
      --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
    if command -v curl >/dev/null 2>&1; then
      echo
      echo "GET ${url}/api/status:"
      curl -fsS --max-time 5 "${url}/api/status" || echo "  (no response)"
      echo
    fi
  fi
}

cmd_shell() {
  require_running
  exec "$ENGINE" exec -it "$CONTAINER_NAME" /bin/bash
}

cmd_cli() {
  require_running
  local args=("${EXTRA_ARGS[@]:-}")
  [ -z "${args[*]:-}" ] && args=(chat)
  exec "$ENGINE" exec -it -e TERM="${TERM:-xterm-256color}" -u hermes \
    "$CONTAINER_NAME" /opt/hermes/.venv/bin/hermes "${args[@]}"
}

cmd_config() {
  require_running
  exec "$ENGINE" exec "$CONTAINER_NAME" cat /opt/data/config.yaml
}

cmd_print_env() {
  build_env
  local key
  for key in "${ENV_ORDER[@]}"; do
    [ -n "${ENV_MAP[$key]+x}" ] || continue
    printf '%s=%s\n' "$key" "$(redact "$key" "${ENV_MAP[$key]}")"
  done
}

cmd_materialize() {
  local dest="${EXTRA_ARGS[0]:-}"
  [ -n "$dest" ] || die "usage: ./run-local.sh materialize DIR"
  resolve_context
  mkdir -p "$dest"
  local f
  for f in "${CONTEXT_FILES[@]}"; do
    cp -a "$BUILD_CONTEXT/$f" "$dest/"
  done
  log "wrote build context ($CONTEXT_SOURCE) to $dest"
}

cmd_update_embed() {
  context_is_complete "$SELF_DIR" || die "update-embed must run from the repo checkout"
  command -v base64 >/dev/null 2>&1 || die "base64 not found"
  local tmp payload
  tmp="$(mktemp)"; payload="$(mktemp)"
  # Deterministic tarball so re-packing an unchanged tree is a no-op.
  tar -czf - -C "$SELF_DIR" \
      --owner=0 --group=0 --numeric-owner --mode='u+rw,go+r' \
      --sort=name --mtime='UTC 2020-01-01' \
      --exclude='__pycache__' --exclude='*.pyc' \
      "${CONTEXT_FILES[@]}" 2>/dev/null \
    | base64 > "$payload"
  awk -v mark="# ${EMBED_MARK}" '$0 == mark { exit } { print }' "$SELF" > "$tmp"
  {
    printf '# %s\n' "$EMBED_MARK"
    printf '# Self-extracting copy of the build context (Dockerfile, scripts/,\n'
    printf '# skills/, dashboard-plugins/). Regenerate with: ./run-local.sh update-embed\n'
    printf 'exit 0\n'
    printf '%s\n' "$EMBED_MARK"
    cat "$payload"
  } >> "$tmp"
  chmod +x "$tmp"
  mv "$tmp" "$SELF"
  rm -f "$payload"
  log "embedded build context refreshed ($(wc -c < "$SELF") bytes total)"
}

# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------

# Shift the subcommand out of EXTRA_ARGS for `secrets`.
case "$COMMAND" in
  secrets)      cmd_secrets; exit 0 ;;
  materialize)  cmd_materialize; exit 0 ;;
  update-embed) cmd_update_embed; exit 0 ;;
  print-env)    cmd_print_env; exit 0 ;;
esac

detect_engine
[ "$DRY_RUN" -eq 1 ] || engine_ready

case "$COMMAND" in
  up)       cmd_up ;;
  build)    cmd_build ;;
  down)     cmd_down ;;
  restart)  cmd_down; cmd_up ;;
  logs)     cmd_logs ;;
  status)   cmd_status ;;
  shell)    cmd_shell ;;
  cli)      cmd_cli ;;
  config)   cmd_config ;;
  purge)    cmd_purge ;;
  *)        usage; exit 1 ;;
esac
# __EMBEDDED_CONTEXT_BEGIN__
# Self-extracting copy of the build context (Dockerfile, scripts/,
# skills/, dashboard-plugins/). Regenerate with: ./run-local.sh update-embed
exit 0
__EMBEDDED_CONTEXT_BEGIN__
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
6tTzvcc7w70Xh7t7zw+2+n2U452OR1LMm+vPbm3c/03r2fbTp3uPhtt0VG4Pn23/29b91t6zF8Pn
L58ND7+FgnhAg9l7sfP8m6fbB6Wfn333tPTL4d53O893/8fO/sHwxfY+Nb3zdPfg2Rajt9jASBJ8
fjh8tP3o2x28cHhA929tfF13effx053h4eFTnN17z+kNv6X5+c/eOf89Pl6D+PnecR3/f/Dgy5L9
59df//pT/c9f5HNH1Nv5cuzqe3LWHInyzNCPICBUNFwEXpCau0AdAa7eKbEd328/fblzwOzfq8jg
u3yZWiPN7wCM+SpS0lVTgC0diHqXhUrgvmrGyXk6OfPV4aglKA7pmOXwCSRkLvLmziAWvInx5yS0
uoJ93OkXL795uvtIfB05nyZFeopKfCq3D/hxSwGn1kKVl9Vd0b2OA11lkPzJVHQ+YNJcUsI2uTv4
rFDy+R4Zb1BPMnwr9W2F1iBV90wVgmB+Jxg5DlQa0hRFn+pOcdU0eqrE8wkbvLt1p1mvT1SvD9X6
QUtUh+lkyBQF10M/QUnl4Tw7y97BD+bI6S+gJ/zn7a9aYqHYFxQGFjmOVszZkcQO0DRsJvs7L55u
P9oZ/rB7+C13Y3/n0e6LXTo8Pp0Qnz6fPp8+nz6fPp8+nz6fPp8+nz6fPp8+nz6fPp8+nz6fPp8+
nz6fPp8+nz6fPp8+nz6fPp8+nz6fPp8+nz6fPp8+nz6fPv+tP/9/VOzzEQCYAwA=
