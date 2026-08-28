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

# Failover: when this local instance shares a GoFile folder with the Render
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
# consumed by one long-poller/socket at a time, and the GoFile folder is a
# last-writer-wins snapshot of the whole /opt/data tree.
CHANNEL_VARS=(
  TELEGRAM_BOT_TOKEN DISCORD_BOT_TOKEN SLACK_BOT_TOKEN SLACK_APP_TOKEN
  SIGNAL_PHONE_NUMBER EMAIL_ADDRESS EMAIL_PASSWORD EMAIL_IMAP_HOST
  EMAIL_SMTP_HOST
)
STATE_SYNC_VARS=(GOFILE_API_TOKEN GOFILE_FOLDER_ID GIT_STATE_REPO)

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
  # Optional GoFile state sync (usually unnecessary locally: the volume persists)
  GOFILE_API_TOKEN GOFILE_FOLDER_ID GOFILE_WT_SALT
  # Optional git state backend (a private GitHub repo). GITHUB_TOKEN is
  # already forwarded above and is used when GIT_STATE_TOKEN is unset.
  GIT_STATE_REPO GIT_STATE_TOKEN GIT_STATE_BRANCH GIT_STATE_AGE_RECIPIENT
  GIT_STATE_WATCH GIT_STATE_WATCH_SECONDS GIT_STATE_DEBOUNCE_SECONDS
  GIT_STATE_MIN_PUSH_INTERVAL_SECONDS GIT_STATE_SEED_FORCE
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
                       shared GOFILE_API_TOKEN. The opposite of --isolate.
      --isolate        Do not forward chat-platform tokens or the GoFile
                       token, so this instance cannot collide with a Render
                       service running the same bot/backup folder. Chat via
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
  [ -f "$dir/scripts/free-storage.py" ] || return 1
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
# Optional GoFile state backup:
GOFILE_API_TOKEN=
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

  # GoFile state sync defaults. Off unless GOFILE_API_TOKEN is supplied;
  # locally the volume already persists /opt/data.
  set_env GOFILE_FOLDER_NAME "hermes-render-state"
  set_env GOFILE_STATE_PREFIX "hermes-state-"
  set_env GOFILE_SYNC_INTERVAL_SECONDS "300"
  set_env GOFILE_MAX_ARCHIVE_MB "100"

  # Git state backend defaults. Off unless GIT_STATE_REPO plus a token are
  # supplied. When it is on, it becomes the primary backup and GoFile drops
  # to an occasional fallback, because git uploads only the changed bytes.
  set_env GIT_STATE_BRANCH "state"
  # Change-driven saves: notice a change in a few seconds, push once the tree
  # settles. The interval is only the safety net.
  set_env GIT_STATE_WATCH "1"
  set_env GIT_STATE_WATCH_SECONDS "5"
  set_env GIT_STATE_DEBOUNCE_SECONDS "10"
  set_env GIT_STATE_MIN_PUSH_INTERVAL_SECONDS "20"
  set_env GIT_STATE_INTERVAL_SECONDS "300"
  set_env GIT_STATE_MAX_COMMITS "200"
  set_env GOFILE_FALLBACK_INTERVAL_SECONDS "21600"

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
    # The two instances need a shared meeting point to see each other. Either
    # state backend provides one: the git repo's lease refs, or the GoFile
    # folder's lease files.
    if [ -z "${ENV_MAP[GIT_STATE_REPO]:-}" ] && [ -z "${ENV_MAP[GOFILE_API_TOKEN]:-}" ]; then
      die "--takeover needs a shared state backend so the two instances can see
each other: set GIT_STATE_REPO (plus GIT_STATE_TOKEN or GITHUB_TOKEN), or
GOFILE_API_TOKEN. Add it to your .env or export it."
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
  [ "${#sync[@]}"   -gt 0 ] && warn "  - each GoFile sync overwrites the other's /opt/data snapshot"
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
+fP58/nzf/Xn/w8Lr68AAHADAA==
