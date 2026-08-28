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
STATE_SYNC_VARS=(GOFILE_API_TOKEN GOFILE_FOLDER_ID)

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
    if [ -z "${ENV_MAP[GOFILE_API_TOKEN]:-}" ]; then
      die "--takeover needs GOFILE_API_TOKEN: the shared GoFile folder is how
the two instances see each other. Add it to your .env or export it."
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

# One bot token / one GoFile folder cannot be shared with a second live
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
+nz9t7n+f76ijKgAqAIA
