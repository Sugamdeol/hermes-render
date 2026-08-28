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
z2+f3z6/fWo+/z8ZL1e9ACADAA==
