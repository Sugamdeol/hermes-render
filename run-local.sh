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
      --tui / --no-tui Toggle the dashboard's in-browser TUI chat
                       (HERMES_DASHBOARD_TUI; Render default is off).
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
  set_env HERMES_DASHBOARD "1"
  set_env HERMES_DASHBOARD_HOST "0.0.0.0"
  set_env HERMES_DASHBOARD_PORT "$CONTAINER_PORT"
  set_env HERMES_DASHBOARD_TUI "0"

  # Free-tier resource guardrails, kept for behavioural parity.
  set_env NODE_OPTIONS "--max-old-space-size=128"
  set_env MALLOC_ARENA_MAX "2"
  set_env OMP_NUM_THREADS "1"
  set_env OPENBLAS_NUM_THREADS "1"
  set_env MKL_NUM_THREADS "1"
  set_env TOKENIZERS_PARALLELISM "false"
  set_env HERMES_AGENT_CACHE_MAX_SIZE "16"
  set_env HERMES_AGENT_CACHE_IDLE_TTL_SECONDS "900"

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
      # SIGTERM first, and wait: the sync daemon uploads a final archive and
      # only then releases its lease, which is what lets Render resume with
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
ZwAgAwA=
