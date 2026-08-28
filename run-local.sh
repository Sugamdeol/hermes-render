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
  args+=(--stop-timeout 30)

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

  warn "this instance claims a shared identity:"
  [ "${#shared[@]}" -gt 0 ] && warn "  chat platforms : ${shared[*]}"
  [ "${#sync[@]}"   -gt 0 ] && warn "  state sync     : ${sync[*]}"
  warn "If the Render service is also running with the same values:"
  [ "${#shared[@]}" -gt 0 ] && warn "  - incoming messages are split or duplicated between the two agents"
  [ "${#sync[@]}"   -gt 0 ] && warn "  - each GoFile sync overwrites the other's /opt/data snapshot"
  warn "Use a separate bot token locally, or start with --isolate to drop these."
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
    log "stopping and removing '$CONTAINER_NAME'"
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
H4sIAAAAAAAAA+xb63LjxnL2bz7FmLtlSYkAitJKu0eytg4tUV6VdStSdrKxXVwQGJI4AgEYA1Ci
16rKrzxAKk94niRf9wxuJLRaJ7ErVYlqS+ICMz0z3T3dX194Grl3Mpn4gfziD/vZwc/Bq1f8Fz8r
f3d3Dvb3v+juHezsvj7YecXPX7/GcLHzx22p/MlU6iRC/BlL/W/8eSHUMkydh2OPFaHjFfpw2LVf
t160Xoh3MplLJXpTGaYiCsVAhp5MtkWcSGvs3ElP3PvpzDwWaRQFfji1eWr/IcVTJdKZFFms0kQ6
c3EVZWoglXQSd9aZMXHLYeL+HH+Z2iEmC2GJnhhnoRdIEU3MAtYkcjOFRdWdHwRKzKMsTPHfhe+Y
R7bEqknoBCPPT1RJKIpSK/XnUsRO6s5oqzMnFYmc+grj9SbNIS5PboSSyQIfndAT3yxDJ3GYkhBY
PY3mOH208GmsHwo3Cif+1F4680Bs4uE8jnDu9EiEkkhE+HWf+CmYiJ0nQnp+qrbMxs4SKbEtPE6k
irLElWKaOYmXOD5ON4kS3tfUSeW9s9wWnqNm4wjvt3ln30ZnEBXJ0M0PGoooTv0I5y/eppjNY5ie
PuOG4qUFiVotwYE5S+yfpPBk4I9lgjnBUniRuLq+xSFBJAh4Lx8SJvBBnFyc2+J25isjOHzQnMgS
6YGWk0A2XjNfj3KS0BUBgSUOkVNCzaIs8MRYCoeIKdeHtoBWFNOOsHt3Fvmu3BZhlAoVzWU6Iwq0
htYhLwKbHfwLhZNBUE7q49hYaOy4d1orbyCymkamzlRAIaQtvsnmMTMWJ5BxEC2hzxg3TRxPmotg
t3qDb8W7/uCyPxydX/a+7Zu7Y/tRJ8RukybdPlzs7uwe2Pu4U2eD60vx8mOVwmOLWB8ld5plWu7F
9nylMqm0uuLSLeiYtP9CFyDLE3qZOmPQmSRQT/AulG5KvMGVnUWK7og+ksIx+dr6IQ5HhLT4lP5P
gvuEWwJCbjSfEy8gpyUk48/jACwaQmm0BmiFp1HYkRL/qCXd751e9vkA2EMK0dpGMyG5+/Cw3DWt
xKL6oBn1AXc9FZlvpZnfAbUw8uRoHnkZFLQDxfBjkdAdBhVWL6KZRpk7w0HuLG0n7L+pQ9KhJLVc
P3EzXDQx0uRHGDXSo0akeXJzq0YEZ0iWPH8MJYBhYVvhjHF7hbmYNqjxfSFejbBN0PMDbxRKaIu3
uQW+pFmCQ505gZKt74f9AW+5Nfj+Sh9fWAOht3Oo/4gObqvRlI4+e+1RlQnip5b46isxv4NdE1bc
MLUTQ8khS5XrHs7c8WDfmsbyc01Sc+B30OvUWE5Uip8nVurk/DVLfjY76GrcQrFiHxrt5ZYQrA6k
o6S4kxLCymK6qN3dNzAvSsH6aWsAg+TA1HusjlFISg37kpDVwnWBvcL9UjCVY/IwmCPucQfJXBIF
0kYXZkQ+zBzY/NyGsdUc9C5JrS9xj6D1EfZBxip1aEkZLvwkCulSWLk95BXIsUVEdOaEU7qZ+Q0H
pbGcOQsfu+SdpuSe9G3lu2UzB1jZYMvIMQRR5sE8+xO+dIY3Wk3p5tIKUm2Tnjq5A9e3PDdmOBmb
UKhzmIJSQIdIyZgX7sdmva1KxIbtWXTGftiJlzgL5Ce+/nrj5v1Gi40OHOsMvgMrxbiB4gb/bbXo
oTjm/2y2q8SMJDswA3a8bG+1yFhgJE2wwRZvRA82ZehGHrh13M7SifUG4yJw4Vi0RzCcV7ejk97J
u/7osvfPo+H5v/TxAlrwU1h7eX560R/d3l6Mhv2TIUYAa+7YO0K8EHLhu2muKz4BDeL/2+7sp7Dd
CuU9Bm9sbHhyIka5e4YdSUdgw2bozCWMTQoohAFOFqSHZFK3hPWW/h7ynYDOHxaXQ5sHMXceNrvb
NGYzAlrR6mJPZcokt4nkpqG4hR+eLh9cGadi83YZy36SRFj0ByfI9OettSXM9FarVd/7JIicp3fP
b3n//OmZE9g723rcn3CKp2S9LpW28atNE9rbonuw1fqkbjTxivfVRLg6+frqdIgF/kKq1dpqQW1a
uJ2kq3TPCHJAm/URcblw2YaMuvoPfrrZlg8xvHXFvLHdqhgVojEhbAD95wvCgFLfEHayCayF48pN
LAhwJO9x1K1tsX55bt7nBrXAF3AvMoANIRAjYAEAs3wXiGkJm4hfv2Qy8bG1Do/T2ESDYBDKYbDS
QQDMpR8C08Fxxv7oTi6NqY3m5Inxf+InAyx8hksja+nFEYSntgkx4gUgshLfAEER+M5AkwAPL9oL
01kSYZ8wq3MwwSejugLHYSpvCN6DWMUwmrPBGhIgX65heMeD/xbjZR1WsfUfkz2FA3MJxUtPW2KO
IIRyEz9mNyIfHDIjYW6d08JaGwNdsfQGSxgTLTJyKqEkI2k2bLdOrm/eG+qqw2tZzHsr38cSBlMb
Zo3ELIq6nh76vB3/fFrG11epGXzlBr5WkZGCLrgzWpjQdmbihmgy8V0fYYlxpByt5fEd+48pVCgb
25BuvpdIqY6O6kg7wOJcooQ6/dQWl8+EfzSJIr9tA29BptB74xf1NEvHR0F0b6I2xkQMu4HnNeyD
8ItogNSVgCIA79VpfzAafnd+cTEcDfpnYtMx2xPDd71tii62BdR3nDihO9sy4QWrh44lVincXB+v
H79p4Nlxd/zm1V8O9l/vvH71Zndnb2+/OznYfTPedeTrvTdvdvYdd9fd3fP2WQOUTIUlsyMjwnQe
H7dfbs7vYIViYXlb7fxNlgTH7Vmaxuqw03FJpJHj2RXpvPy4vufHTuok9vTXhpdnjwVpF7SFNVHD
C2FZMPO4jHvCikT75Ufs59Gc1tak2vQYE8rplKuxHn6dPDXeOsnfFFM4uCTbSoed+GB+PkJY8GNA
Wbh6u8JK4ZOEJyzyXmJD090QvwGfOvQU5rTchQQcxCMQKqiD3G+/iY9CAtiKtstRLBttWlFT67AX
cJIxbGtbvP1q9wibg5Z0j8RjTjoPtC2PuGJAsTUtPs3Fzuv9/Yb7arQ4V+6C35CsU99ox24/P79T
EPgETv+cDSRzYSWTdakwmxr16K+NCiTefsambYOAcTcvIrgwTr0EzvKwQMGWgqP1YYjyNIYVhVYe
f2qLxHF2KrXNKRML9KmSF4LVpwTYvQ+nIDZhXDA5Fu1cfhjZZqNWNTh6u9oC6FVA8+TinIjVsi7O
WNGSRNaLwo2UNpJwLsIPFxFiDj/dssWFz1H92flgSBmaZvtHO4NGs1pT1Gw4An/NKqpmjgeTV2yR
ojSfkhzsgyyLxX9cl32uzU/KIyDed0gKp4UvjYMMMc8mTm9YlCcgXhpc9e76st/Ro0DbmG34ZLKz
+IT9ObGtZmKT4xhc3RB/gHXodASUKC3CgZ92vzpnZxJSoMHhJ5hm9gt4YpXYBf6fM0btnueJ3s15
gQzawADYfJr7jhIbXGowFJvMySoMwd1HSAUPT3m9XzJoCcWeELxF2T8K/ipJw09yu1jSKpizzvj1
QcT9gYyjnFHVsPQQuwstJV0Y4BxkK5KQZt3w+mZoATkmyzhlndEjde7DFt9EBPQSShSaIZ25TGiJ
Umg1kTFmYhGY8UyIlL7IVFqWhl7gkwv4udS5xJhg5/1McjgMDwp7CjIfzHYQc7gUd3wAXxVwGlAi
nuMCeSbunQDKz4QLMVAOVIMNON1PKzcBowYG0+P6NErsHNKvAqkpKSHecNEMzipvSTbE5JyDetWV
YxXczFQlw0m4WagsjgPijaMMJYpKRt/139uU3Aw1lsVFp0zIEU8FlHHvVEbJRHoBC0jTpw5dRqPb
JqOyAbHA4ucT4FsRXYghNIUXAqTZ3T/gzTHA5SwZ7kdskqoMhRkyT8lLEn040G2I0adMnaK4kO+F
TvHr1KmhZVn813KSaXWx4681sbfCtjVi4pc/9AfD8+ur48We3d2z98oXZhY/uMWvPn6dvGuCQJSm
BSz4WI46tJy5d/CqAlgoyYQhNBReCKfMk1088DcnmR+82hJHR8Xzf9gy3i0LSVBRQhpfrnBsaHFI
o6JY1aBASUgqx30eqo2xvRKrVSAaAnEi3uFfRrSwExAtgTmAtCoPfwc+4w3jf7Sw/q+1QstG/Jc9
2DnLCmQzET8a0FSRElj6M2toydccGlRHCVFb/zfyW3hOCm25wip4FkBYOZ3njlLT8HYlh/np09Wn
lWLPQ3gS1BTRuGiL57nz08v28/upYFB8djOosLchNnC2CiZdQaV6L3VQCpdUmIFc+URdWE+g0lIo
BeXPlMjEX0O2EefECdTqvwbS1jWsk6mko3EEhan08BlEyaexLGP18AlRK6Vd8ycWn51M7zdFJfAe
DiqGKaLaG9yOrHikRM6j1NTOtk3JkKwtG3bPSZ1OxYFTtstJ0sr0sua2rbWbfL+iGLgEEzXQteQs
CKUFYGI2gRB88fd/+3djHzvlCPjTLfs5T1QDTOuuqPr6OVI6D2DO+omsQzHiOYITKngSt+HNmgmu
jDDVk3nkPRX7PHPc1SNUqxV5KuNTG3jemd8gEtDYI4cxOpljCs2m+AvBE1LkgIoLDcAm0gk5z0Z1
O8B17fLtWq2B0A2ubkxqrFKucfrqziQ0iuJrAZ1AiR6GqcbDnMfDhDji22CLHrble5Xqgpg7S11G
B2Yi0lwLBZm8XqEBLIEQej+Z4G1erNclgt8TtNLVYfyTYlWcTOuGLj0WcuTMm2YUPc9r/AUBKtYw
u0xwYO7Yh2mkMpFHc3ntXAteC8MMlA/SXc3LVS7hpgYrToB95UVK3icFblxgpmwm7Y4X9JIoxp3s
X90O3t9cn1/dih/bbL/IctFNbm+LtjXl3xb9/rQGt39unVyegobJBdMM7AKP/4D+j/xW/gGkix/q
8oH8n+j/0Z9X+n9e7XW/EPt/5Kbyn//j/T9NHuN/eo1Py3+3u7f3qi7/7u5Od+f/+7/+jJ8XX2p8
RQWTfmkBDTAqbGjVWOWWn1ppBlXruHlzfmp1YbcplosAdGD6tmxxHZqqC0ezfqoDv66N9VSW5LlE
NusAnipVbGZ9NrA6kq1F6LqdZJd85Cpm06CLSwm69UmP3eOmldU2NOMWKKIKaNo9OQbtUFWRPGDP
FrqG0CtbH3jVq1AFST81Ho/avPjgOZ1iFT81bWQiiO4pT3QXkh8pdlU0geVpmSNB8xLtGynlyPlH
3UNm57TO4R6niW5bMBk8ArAEJct2NM3WEPNTiwp7eva+Ta6YsCvXaGqs1DURgiqmEY785oZa9Zw6
QFjFsFwPZJqJP/WJJsJ6JfKasCAfB05/KBo6svDDltaqWnWyQpgdMsOCqtKwU04I/Zt2Ji0EAtjU
bUeVIXbTK710lKtzpVKQU5YYsEHAR7eFeFnoOYRyQpMEnuFCLLk3gRtDZoj7qX6p2/AQLgXSsDDX
CJxH95SwBugi4p1O+wLRhRupSVnNtJovZWq3WiY/0Wqd9m57o9PzAWUnKgnSQ6s492O7ddO7PcHL
4wZAsYJ5263h7fWAkkTD91cnTRNWMG+7lbe30B01ddHUYWhbuaUrOs+4jdEu56oZnknwHbeLG8uq
DLLFeamaf//X/yBmMd7N7yBnk+KZnMvECZh/eXKQb3jRuHa0CmhVM6K1W0XnFJiaM/ixZM2gT5/6
o+vvjrtUu//yqeJHbbrYfdvx5KITZkGgMxmtPFz+scrhn8W9k4RA0YeirA7pBSrkjrhrDjE5BnI0
3pr4OpXLxo4KoIlcUFNkYZs2lDF8xoJJE7vm0oQo7vDCV9zSl/dnsikz8gtibcHy/H5UFs1Mv5sT
UEMOLLGzkHm60J/OUrbR2jZAws4igiAcyhdHXOR0M8QsC2mOGela+P0sCow24YZDLDoz9MA5n4qW
lqmhFmePvhQVeN8w2MTwdekUBIRoEPNOpRb1lLCqiYBiDSrzS68qrCJaKiN/LT/KgRghPmXXFHun
ahextvlkvk2gAcOQxSKG5WBfRP6MrA4bLB04mhgqNz8cppCKcSdNzZ8hUuPKC3U0z+Gj506QuzM6
D4y3bhsNltXeXcejwkUUcvr86uz829HZ+UX/uMrtakqk3fr+Zng76PcuR3r4ca35yw18q1oBkQ8O
tZe2tTJ8KSxO8FQWIm2gvsEfzasV6qvK4sbNg9aIFq2Ia1e8Nq6iR43qorgFVNRnlYaqbDU2QKDd
0tnKz1DA0lrQIitLrNuLir5dUlkGOttcBSr7f03Lb+2lsdv6rld8mm4qrOqqysYq9dOMlUMEPnxG
UcG9PLkZ9W7OqTTxSM6BywwqChYGpuWdEmRpTD0KduheQkXxl4cU7er9yu5SZ2x6uWFkwHmYQbE5
g0WiHCjfh61DPlpA5sd4+ur5TCJAVyzgzsA+UKvVIqkIUxlGbNR1EHO22+vriyF1fg36t8PR2fXg
pH/cJb8HKFCUVOIIhiHkqqvQBItCyP2MG1qooJNEpucUQ6e41vfU86UPyP1xiuttui+FlCXvQyoq
cLqiRoclv8Bewl9wOmoeT5TlMwDh4gF5wBVgneNtsnQLsivzOeClqURPUqlhE9f47Naw3z9thhyV
nFgbJuLy8vpq1L/6oWkolbtI8FFIPIEHNkzsN2OThupYm7CS9EakIiM6/OaW+Niils6XXeohDRyf
m89hB1MqpnHPnOhzG5p8oKKMIsChUo9sNkFDuYAZNC1g1P0GhAJ6vAgB1+O2ZeV1UCxh0Ukt5nrV
AHLhzrLA/TC1zDrtlil+lO0ODapzaO3AGh2LdrdqyWobePmx+M8jFsGmXbYjuOx0cAVHHnCSW5jU
9/HwZHfnzQHernpOFiLWq5JsPbZqrjgfUzO7pVxXLa45LZU/6pJZm1YBTFs1w0oS0DUGovTY1s1I
IYFi8+y/YjVZcUVd5T5hNp9lQUVZV3mAIb8WpStTkj20KgTW3rIVN0NMXppHsgfsyNTNFb9DuJxs
xc/PuaKyzJ7rK0eNZNOh6mFUlJE3qxvZOsr58JSHu/PjmICBMTrca8H9mnl5usHMMorICctAg2rz
tRJroWs2pTZAM77qPne8QsI8WffWmoaSo3KTa0woN2HU54Xo5dBmqD3PmelK4KCVew7K+i0FjJS/
p8q7thHU3FopbP735S4KJXtW7kKs0TpumlSWJqkff22OqdBpdGy0+vbyZqXUXGkDW8NIlVmP+UBd
qNnZaX4PblWNEdnLSj2musXjJoY+MbbAos08rswyZUKjxPjkh3GWmu4/7SssCz5h9WGtaJSPWD1i
xUst2uu2oilSNDz5pPV8coma+XzahDaZUR6d34Xfb05L5GH202RRK+pVWeozbnW5Wi6nVVaWCzSS
026Qky1VjRBzXcc1gM71Y18DYWHTujpwKWibrZf15nVt1lytPK/C715hK4qsHkwkCzUiwEpRdxkG
M6CbO5xrYzDGkSbCaWojM9W42jc6GYjrQLFsS+O8oEaLEVXuYm6IohKj+RIqlNluIQS+/h6gY3TZ
G3zXH9SDOLvKSovzQmbb1qLbbvVubi5wtQb9/ui0f9b7/uJ2iDi6FratkK95yabpXcMw7tyvNcbR
91E17KWIuygS6sSa7sKX9N0RTiAVvZqU6HBC0/NFaac8Nm/+im/+FWBpvlTLARveGTJOcE/9koh/
KeKG68q4plrEdgXza5kMk5tbT2LUIGATNzC54XHFiq2AuXKlJ2Pxz/ap1az2U0kOKD1MvO4g0+Nr
Dp5xbvMBLPmL6NYcGX09gr972Kg1T8fma2NrdrDxjI7pmWv4wrVR7/bvs1SlkdIZz5xcflvMZV6x
J/hlVvg09VKu5Xd/TA8kp+GzlBB+iXiqycLzyre1a92yFUNR7cd9qguXvuNwzymm+vdiFK6F/h5Q
8ayDFfwJbJb9NwWILc7NV/kxLtJfo1/q2F8bNUzl5h5q8vzP9t51u43sSBM9v/EUWSm7BagAkNSt
3JBhD0uiqnhKEnVIqtwetQZIAkkyLRCJRgKiaBV79a95gFnzDPNgfpITX0TsW2YCJFVVaruH2e4S
mLnvO3bsuAfMH3wNUXOtRa/ncyQiNHtkJRaB71VStJRN9hyljYmv144a+ebC5/uGvWzkzYWYXa6z
He5aLRhzyqGYsJm4KfNMz+BIyqIcsbu2O8RG4U5cjvesfBknxJRPjXlR03lrc3NGigBjvwgO7tTP
ErYIr1+8+W73FbGY+7UMdcVcOLY1nh0chteBLSHIjd0nvObLCM6XsXtthtTPmpO9so53wmVIA6X7
B/l7ukH4gBFYzXCbl4e4cW/jCRFyeqzNLGZMff/0k0Fw5thr87CYBz0GW0B2CpEqLUNTsX01fO4A
3HafNzy4Zkl3kUysb72A5diY5tqG9IrBcWlbfVbEXuoqgOfbJp9wwAnef22i6DoaMhSQO7LFX9IN
moObXkAa00KELcB3RFqYyeUSNFQmPaubshmSgGM4YOtQ+ZKo1Ki9IlYgSudFsBqnhcB0M+xebtWK
zleL/kuY/saCCmLTZ6DcwnE/qQyF9T0XitkZ61Ukvz+k6SxKrO5imsyKU5iSs5v8mBXRU0Wao/cn
HHRCUKUqe9lOO5mL+X+gGcoWxnUcGiGFTpp8QdgwUBOKro5uAvaQPKYTfMFxPFTBLOpkEKvGCI7V
V0ZnNUIADlrgZOKoWU95tU5nJGy098EpfGoA7E70LS3BclaoS4PnhcMaekhSc7rfQcgnU1+BXFow
bku8OjsJ7a7Q5/+2zNKFVdUViKdSIsxpofIzSGg8nXa3JLKbZoRlYP+7WTNhvSJCpeQ/rSIwdFvd
RtAsO3aOgbKLdVgKUN9jv5z1ap0aqxvqt9Q7FnavE9bGQyRN0ALmkbCbF8rGA0I6EY4GT0YLIrWt
vh5UT7fBpnu+JqnOQpYW47/Fv4a53H+5Z4VV7i/ax/r4Xw+/uf9gq2z/9eCbR7f2X1/iufPVOr/w
RhzHe58XTarbaPhGzGC1MyBAvTySxSIhtq9k1FwQw49bwGF6DSvQ0M6T0YjNOtikoS2mIhIdx+gV
YbuldmBCLom7tZqKQS03Tc+hJTTe31wW3g/QFCr57b7N1dCFBpoR0ythEZYz+NDQh4Zo1U3t81OM
0YRbKSB6wB3Pd6DEqYEr2mg5ZwNq2+cFrp2UVZAyZJZSgbA4MlcT8xY0seUIEna6THUM3eiANkLu
rrN0keDykNuH+QnIPog9gFO7RfG4w+Fxz9tyzYuJF4l2FAjeDFtNBYw1HLT3Czv5kAhArJPRZAns
3hgO2WdvKPuiiudJfiJ6ZY/HTBdYEXrtkQJtE95HyOQGpDRqeG9IkWJJgPYhZeKHqXOoIehqQiHP
cI9hS4FKAKVBZA4R+kW2EEAYJctCKJrKjI0INnrFJvhgowhQLqLXcmg0aJJI8dj3kwgWOkkSJGcw
OF4uqPvBwMTJ4RPB+t+i0TDv5ifUJhGQ+jdoNglIRYu7MG9PabmIYLF/Lhaz7mgCmaZ5BW7c/KZV
Rtgh82derI7ao7+L7IROvv2LBml+08nAets/07NZ8PcpqEOvMw4pJiKzC1bQ6PtvM2LqL3b35Nty
PqGRdEWkpyW+Pzx8rbFi3uy/4F9BYV4mU/jfljkEo/SJI5/Iz2I2yRZBHWwLeydKrX35kwvns9Qu
2HKZjRuNF3vfRX2zeAhw84J+pvNmrDGxTkQ2qpdn3Gp8twfFA9s90Iih0zX+ecks60rxbpbHpuCb
1y/2tp+Vy+oBt8X1hQiq7ijsEt416OQ8PYpk7yWECuNGMSyg5T7Ol/MOYl8J6uwKh8AWekGMKgP3
ejRgSMdtqEgimSysMJWANoNlrM7iT4eDg+0Xh6zCvp8cbz56PKbTl2wexQ369KfdV8/2/mTi5CBK
00O66hsqHBwgVJpE1qFPGm/nZf7XbDJJNh51N6Pmn7LpOD8vIiqwtdndfBLRi8cPn0Qf4XwJQXv6
p/Toh2yx8ejBN90HjyPh9uLmD98fvnzRFquU79LR+7wVPT0lSEg3tu4/7m7i/6KD5DiZZ1ozbrQa
jcZokhDa+47XnkGuuS+oyo9UREd62ywUnMYJVc7oDNM9kCCui0PWXRx+0+iBwMlTXnVpiGM0ER+c
LQaDpmWoi3Ry3LZ/8bZJwCb7ThDXIBvXv3chnhyTPk+Ps4+ll7CUmH9IJhy+yr0+Sz4OFPUNji4W
aVH6DuQ7kOCGYXsT4oGW9KH0+nwxAPx4bznU1Cu6E3vBpLtisNiXOYef7JTps/1dW4RFNn1/LSB1
NIdWmSK+2OKwvqwRjEjkh1dLrKJL5c3yUQ3zMyxQWUgqWXkXVnFrS2XdH2Ehs87ATvozLKArTt/1
V7l+sRgcZ3DGZoMV3pnoJ94RqoN/GlzjvzHkItZmPrYAyxpKRMcaIaQFbWQcQLa2E/dCCMbxLoUL
iz18ebj3w86ruIU1X1FKSrTp8LVaXRpvNmu2qgeCullR//neC6hfdp9JG5UmsmOxmuTjFjgKalAy
tyxmJWa5EA2IQXa9qHB2SfzYaqV+PidKnHk0zpqLrVbthW61rsqgmnE2JaClVftt8UTtOX5b0PpI
R6aTVQO1UdpK7wkumuFssaZ9Id2DD3bb+vZXbQGWyK7f11fbLxHmrfaYt8JG5Xivau/gcPtwZ/B6
f+f57r94DSoGKDVlzn0/AAXb1J9fPR3svjrc2f9x2w8V92Bzs9ROBSvUN4h4dgglsPsj/f4WUe2o
pege/XP/of4TtuswyKrpuguYmqveyqVxGnSzqrUX26++e0MVsXDptPPmoLxgio9W1VdagqqHL7xW
Wia2IdE9BYfCA1w1hZjphbdsG5J1Ih8G+fEx8RV8IAlDbPKhpHMkp0PKCBZv4q7v4j/NVrSxEVUo
mFb0ddiomAok55Z+YbiNP6ka1u3AZa9nXppl9F7xLOjv2G9DOvJK6fKpTFtOpp46ZQm64o7fpBF1
hRRumhCAre5p+lEiajTtKnKs3uligEgDxJN/xjoSc7x4y3iP/vOu54/pk51MvL2kS2Se/ZXZnbhH
s9NQf+ECxG6j43/p/Em2uHOIb1Spds9Lg2sFLXz7IjbKbbvqXoE34CM5wLkr5nbMK7g9AmKlQvG9
jXv+IPfYyQkfbCwOS+d7xfZTeE/Pa8ttaMFLsycIoUgcSRMMml4piMV40fPWOj/6C/HY78JbO4Rr
hMHoA4xCvuTyE9p1VlnStKPuqNbXRMv/MSZIt/xUk0sFAEffzIDBbA6Ut5JDUA9G/EnICY8adNPk
P+/JP9ebsZQ9ysdUVEis0mcLojJDBXPE+6yH/JZZFrQJTh5EQUiqaoW38VNpoYOopvE7MEBsazBi
GN/AosS6YMJ19g3D2Qy2WHe31WbpSR8dt00nff23rcvWl39airhglCGsa3NueFkgL2LT+g8ftdhD
U5kTN/5ZcgGGkobDKnsWaTVNMQ7A22zZZWA7y8KIi5pat81LyuSavhE0jltyWRAd9xUtRv7eIwIl
9KnPWB3HykJ9kjldRgKYqmjr4U9u+qv5ZSzjYelSP+wS7+IAMLkUDd0bNl6ZMXOsmE/2sJnIOE1a
SD1sK3DgOIW7AG9uz8ovQvqO+L0D0YJYHZwRIkH/xuEbZJQsCoHynoA2YtkmM4tohMlzFoX0oh0m
6GAMEZLnKCd+RYoSlcoWnr1Qswow/xz4uoDXLEeSY3NddSQTvKltadD+hAAfLpAQ3llxJCsMTaDq
/NjJIJzsQfXRHMWa8TAUjc3NdtTZ8uL7ekvYLdL0fXOzVfttMV9O6Ril/vcrj25be3ZVqgePNnn1
2Yq/2zmMXe0Kkb6gxQVK3Qzerj+FjzdXnEJbnWXHh/NlzTc8o9Pl9D11Gh5Qj+Kr0ufe0eXa9Q3j
OaLG3td+lbnSRTDBRYtWVvYjRf9g7s+q+GBl91WksLIonrh0pAg7T5L5iWT0EBVtvLYBR5ZVWXMi
9oJVvYxefhtNsrNssbrN+iXxAZktxOrWT3EVr539YMJk14g+29Hegf7w1othi2qFS+wQCMENfQ1Z
lgAx0te2k7S2VDs/6uLOj/r96OHmw+r2OahZg9UNXnXo3A0L+FzR7xlxkBmk6AMRc64jH1jS7/HZ
uoEOGdtXgyL7a8pkapkAcBiafZtM73rdS9A6oLjxUm9yjn4jrqsW7qacB8Dga4Mwmdjq0BPKhz9B
mtzFfx42mQS/jC1lnhSjLFOUo7wuj8bnJY4QPIZoMdsN/Yz/df6vUweXR3cNKfKMA/5kckcRMj7r
4Op7EolNk3SxO+bq+P+7tomvzfG1nHiZffCK+v3r4GmmnzX0r6Pj6w4eAvAnFgb68Sfz85LbvGsG
HMCrjt5jH/1p+BRcL/Kpt5O/ZjOzSv5ER5Oc5SR9nUnNBDsdV8ncVDTQE06HAHTq7TTYSX5l1g8v
fCDW79ptq6EU8xyZkPpWydGsaBRapnvOBpND/uZpibo48gdP7demtNidpotJPnK319Z9vYCvSTev
Jo2PY3vWNrytNQvX/2R+dccp7+JdPht3W8qoVFp+wSvKbUMiFi60jCa4wN1SdGfLhWFW4td7BxA3
6Pw5zAPkvhuxL1ycw+mnHX2AWA2nXwfTJdx+VjRbIYYMO5KiTVe/VTeidDo2y1n7vaACAdisLmQB
yRZZRWEYykLB7QrCYj1BUSUkygMr3YGVzwbATQGrVOn7ZYnkNx+aQdkli7uvYmiOM46LVgsVGIFp
tMr4SBce3yMvfgbbI/edux6lwQqz4/fj8TrVEXo8zk36F0IkhSWiOBMbc4KaIXkMlpVVKK6iqguC
87P07AhxFVVN3D1M5rvT45wv4dI75Wh6yn9s6+UqnMs8PVkSbSeLgFqeKQE8s1jn31bPahAtsCTo
alMHsAxT+40xMr0YY1q1QTnCJBOwYGyd6Ey1JToCsmhoS6r5V2sSa8R9lJogxerMHhKmXX+HdEm6
WTHO5k0GHftG/Jer+U8si2dUAlzBiuzY7sGsPO87Qn73WIO/jk31l/sFrfMkegAeL51niSzDeAmL
kKev30TFLHufFsY0HifTWI7gK2MUbQj2LTZOse4hRwZI2HSD2i3O2KtaiHT6H7F2al9CC4gA/k6Y
YUCEeSn8yo/+0vcm1OacJf34vHfy17gdmXYmmEz/AVPDhii0a2o2JRmP7VoxoShURDduu4AlfWBJ
pjNpVfol4LY0a5EcpwONlNK0RKgH3s8ZYMONKe0AB87q2zJdtWJqGlrQQAl79esMCAfIy+DOWYAF
YrrLNBZtGBDDFFultrlOhbXlRqjkJGGtwyJvYoSuRkWzVMt5CLpxhQjbzNNj0Swtp1g3CyMi9fvk
DZSwjJwnn2WBZ4E5LsXFWekATabvy9fvuiFMMrrw3IJWurcNsUvaqWAQrgSPUIg75mnEGjM2POIc
cd1om6Pf6LbfLYA+6LC6poYCPkPY9izZOdpE8HSBJumgiRFR9KC7tSUm3tbE38GwAToZNoenPe2b
bbfwqYQCx2Obr9aNhFYEJT5phaEJzB0SloWL/VcofQLYwqDUmOvpDPQmqxET4xl5smE8LIXxbBri
DaXtio1PbObTdMrCCCDVv9sZdP8dlKKrxRLV/qcAMuLzxSodQklLFcMJiMpu1bw+IKIcnzY3N0tf
dZjPebch6Y9LBYp8Tl/TyRgfBYsfEqFdV+wZm9CJqqTjDeOy7fEidDbYaJxpA0CoUiCyPtejQnTQ
0Se7qJdG6p0ohOi5MOCkWEt6NS8JiX66DIgEWzwUOJjXK6SxKYcGVAiuB9xQyUFM+L4GlkF6KheL
N7AFpWNmwxqZqFjORs8w8ggfVGKCK1dzuYAwZMY8tQzkRjApME1shTkYCNOejXVl9aWYL+gfsry2
SJXcc5XacnzVR9C9X7f/VkqTjdX8U8JFTe1UsrEGzkNgBNa/KVlK5CFSXV11ns2cj71J6/kNhh4c
YA+4cf3IimiPsiJ4/dwHyxpSXWqGy6IvP2NNxCNBFoVvbXsq7FUtkS/pYlmBetum+1aXecDg/qYJ
hCKL8mnRoxKUwcXEH4PD33eHf11p3HZSOoRmvHeCkvBa9S1svKYsgHqz8cbvY+qFihdrzlYIOlfY
epUOZOkkshxPGcEx8rm6tS3dBnC5mios7QIlG7gJi0n7r7BkvZr1Kt8Qy6NJNqKSnAnWw9sC2bUy
NWPHvBJ5qIzCuwilip6DNk+a9YaBJFH2Shq/Ep2Ud0qPTR0iXCPzrZ4c5NJUJAxC45OOx/GUV+45
DJYy4hebsVmowCFBG2e7JVsxuIk8CFH6CDISDZe65o6ZZMXiLY6eGjX4Q6y9qDTXpcZh7UdvHU7E
iQkESlejDDcZizR8nHEVlliDIazLuynHArQSfrCWdnCFAHPWNEFF2XxKOnrnz7gLwsUdt/fpRX+S
nB2Nkwhysl7UxD9KNnjET7TZakfuEzF49r1HliH99bxQBs2/JmSPdQiWQwNo+CaOzRpmLKQhNFzi
6DRNZlYGEnlN6LY5Pz/2hSESl9vYF7N3VxMOKghwwA0a7te5VqCsF/8YQg9u5yybLhepplc/Tybv
I87AWJTkIpJIaVrjeCmL44QlXZN4U3wtEslKxy4p4M0vzsDfpOMNZIZykVbNeqItm7HMsG7Czzh3
UPF5x00+0dBIhqLSZEB9a510NEnep/ePmvKBJdx9pGY1x2LOQf2RwKytMhsE0Su6WAmPe1/kM+iX
DLueTyb5OeZR9Bnv+hpnauptj6XEHNApvGqDG8+MgU2GqVtUrROBNjmlMbPHxGkLi50VA11K75SG
ImS/2fDq8zMl+82GHHqdySjwosmcDJhvhpexMu2qOay9b33/92CWaK57MNg9eLa730Q/1AESkqet
a7ZjZAkus7OTLViWtZsUA+h8PpZGLvDRXc7AaVe1wsfxs3/9181PpslL/GHHCOXFYFqEL6HP/Kdo
M/+GHvoytTq4sN/6LVPQYYBs/Rp7N/m1Nm9/57u/w817vnLzgBB+6d00UmsZVdUGUhFYRZC6mjI4
yvNJr3TZ11EVAbGln9wGOMJmmhuaJnBO5NgvT8Qb0QbhScRv3nrwefS3TpRRIL9k0VXf9Px2851c
sXi9khDEx5AGxJu1fJM6VoZkGX5qBhtrCuB1LOJe9R7rHiIg+DyZX6CFpsp4vz6KWaKr5l6erwj0
on3PdEvGbAgnLV8x7WebgBpZ4fXmAt8eGuXiwltwY4hWNmVaLcu29nMyxznk2PViazy1kmYnWG6V
6GOrxwihyKVFCIjlMuEdHBZcrNZCDk6q6YDKrSea2xbEPdK5zQqCQMjohOAcfpVj+WTT6vkwKl0m
uLlYDc9ZBWBXzYGx31TfjihY6woarCDtKmv5ycj8CmYgXS+X9Zye3dd6Pu/Zzoudw52A06syd3iM
nZAHue06f0lrNFRvJyThQtjCZaoxZsTxmMOAEOgp1QoVpdjBVP2QgwYD3xI/lh+7CFMPBg61m98S
qBhYtHtGwzSYGQT2IDfKzStxczu6B2JwPkp7jKJps4QcDHG2T9P31/EKAYrkdoWrMubkoRcVIMt3
qgrw/Dg9Wp6IojiFAozZCD9uqfFzlzNeRepyIGX4YvUitt0BU3b5abzoGo/h7jQ/b9Lf+P1XOnLd
5WLU6v32z789++348Lff//blbw/++2XVOult73fvLk1e7Zuiak9h5qGyVRrLdlC+Vcbv/scu8jKH
DDCXuq7VIfYgrbcwjMNIAAT6vx1HL79ti3qHGR9+wSdicSE2gU8YJuQMIAhbu9IsD69sVFgtdm17
xLBqrTGhu/jxBMtXvqE8ZL6WdsFjWdp+jbmelVYoVIZ72uZlUEkpXa2Cy02DDp2LUs9/i9q7q8VV
0lZIp8i7m9g+lKXfylaPWTEoo3GCqtX3YNtfzraOI5BuVXGFjypK17gMrnyNLyyN2PztWHwdWrFZ
Xw8I/JvbhTwY5PPBJD9pMiqs89fsecOl0QVFutbl026HFqTDEvpGuFlUo4ZocHJgvXQRlZ0/MUGC
IPiiZ4vQ3kQ6M9T6cjqQyEc3INjdIIlCmsHD2MQp6O4g2mBTQVTYAb6WByjZHCASwvJMPKDpnMzZ
+lRcUeq8mKkOnTXbnsRR6Mo/Tf3rYPe7w539l+2gp9ba8ruvDuuKr1ttWUtabOuj+NvCI/vMWx0o
DEIWqwI8AVYzJBHhVODzYtHheI1OWiRJSiSGijbHAQunGUxAbMgSk8sGKZXSaWGCrkhtogCD3OEG
0nhJz5Ns0TzLps2tR9UZVOxn5NZiqzdhhqkFCGiwL57dRJm+q1Ibpq8rKC9rmV1Lgnl2IfrGGSfU
02UBGRXsbGAz9oRuZsS5hWOH0lFMOwXAyCu3as81UM80n5+JpU6KXLUKnybil4nujkBds67khkpo
7wgM0mPawIU2xrZ1ZoSchx0pOiQt2EKj0qr81GiYJzkx5I3KZqzeCCXvWO6n0vZfe0OCzZA5rt4S
fxsEXSEXWzN0EmJb0zkTOBLrpbuthiGv+UtznEp0riyf9gfE5Y4Gg5ZXE5ZMA2NLAjqbw+rjaJ/m
CLXUt5wgWAkO5kb/Cs6MW2taMqtNxaEu6LOknItzBre+qcX/cO4IvRBMjJSjpMhGgnYdnSXGWasc
hve+G7zY+XHnBYa4++r5XtzqLpGBsOkRPLAXThb9+G1gWP8u+m2T/ioQRLWIfZ2AvcXqLsCrbjC9
czbtvLvWRotDnDZFb1io7JmTyCDopQNJapzrmXwH0LmYDVmDfowYKujz7w4DXZEIS6P7ctk6nCSo
4hB5flzSX4m7YE5SOQWjxhFWG8tSS9a7DjoKx3EepRylsltaYN7Z+/VbXbdrfHbWbpk0wPeURVr1
GyiWLltffh/TwLjhCqRVMzsZskd2rQFQu6KcTmXAGvLBgNdyMAAqHAx0PYU4P+CQcTsfcbszomzd
Bm/8Ms+KNO+/aB/r8/8+eLD5+JtK/MfHW7fxH7/Ec3X8Ry9Lp0kDB3mtMKV3o3/f6JqUfl5OuHyq
RGW30dgec+pdTe05PRGLUUlayCHtsoVmWT9BdD0YHJ4QLrc2dmgQyGKrG52NZgNJ2lBoloqo02Ek
6edzYGYFdrkLVNCCQOhKSiRwx6HrasRWJMgPy3khKhnrODnNh2TetRy4pANSbfBvPv24vX8ZpL/z
nILaJuY3cvdIPg9YJ46jSfLXDInVNBiTiW7L2h0Q1ohFwS4KfuA1NLIyG540tEiObGi2eXq0zCZj
Y4+vCZul3GFtDgweoUmDYdtJoiGHDe6KHUE6lCZE8c8z1JXhdBup8dpAu6hn+AYJoA+bYVkM3QZo
62H6nyM+TTfamyHwck6TL075Bl9ozMaMU0zDR8PmohQ7PG2HZdFi0QAD/lK2gg3TLyrNsSawTmhw
CukRnf78bOCC+R9xjgEAlcs4gIFNt3c7MNlIFhkkE8QC8QKwObmM4ts/v9re37agY7OD1CTF0iCn
TKsodGiGLp2Qxj9W0lTSmJwmpfzVJo8yRK+Ttp/7U1op0gmx5YVO4m4hFtn/dp5OOw+6v+ucJR85
t4vU9yAHQTNFF9BQAgXekNlCEvxKaeEweBUfdCXlQgGj9HROfBHIAaRY4mRDHInwPMcEOzUJGmjz
dbTcxgAKo2bLrsBGVExoH23KsGI5P05GadsOd5RPJtnYboLNOe2iUEu7Eq4WcVDpkFp7mw35amml
TlST1ZCLdISepacpEN/Jpx0FRtClk8SpZda0YofVnGVTOFtpobwwRVrukEqX2jo7ueN4jl26V8hh
ZDVBXxWSaHIqa4Igqbw/D7vRU/i/EcPPivya9Ccm1Tj2TJx/OGnsBksUaKfGSC+huBPCz5PEJn9L
lh+zSQaPVwJWcRAYXTiHHBTjH0uEQz+a5+eFAc9JdszqCbVFwjKZpFqQZRbdaEfzcoq/pJ8CU9Um
ND8/8fnMhKmgpdp9dbCzf8hpziW+LmRo3Wj3GCouk/DTJRLJ5w3gLM0normONc11M8VdxHcK6hT5
WSo5UcbZMYL5TBdst0b8bfKBw24SZjcNQd0i7lJ2sb3JcHx5hGIHd2OTNzuGlDe6YawdIRrPMFfj
bsYnXsCokl72CaFuzSXFCUw5+nDDT7+CRFG4/XQ4GCqAx0s2L/HcFQcabJOJwVhDsAuuCjRfQn4O
oXGUXvawZ7yxHpXxhcL7oxjHRzacHKPbYHs4eO3xqe3QhdDRKMtB9uMwo4yqMbtcQ+7X89OclyDI
ZFtN6cS5lwnVEI/CmbWkCayLj8aMBZ5k5tBkHhoierESG7SVVTqhfpBKHuEGaDU58WCIS7FgkpDQ
mFg2Gm+AWV9f/Hkb4U0lp2xxCns+xoGGOmOC7sbBh/PChvy9WBMk2BTC2iFHwAvBTxmfeZOVl65c
OIIGaFTSAk3FrsqiKx45NVPClWZ2E7hyIpCNwYiaQieJkC+og7bGJbyPRGdM1B38sPviBTIjHLjw
sutRvQqSVpcyPVHBVsOjHUtRfEMCFH/GfuntN4ffo7gGQavNmxw39Hh9u32wU25fKBGlW7rZeAMp
2sLjiKyr3Efw1pYywf5e7j3b4aYrJEKsMkxAtIrSNCaZtaF1DmOqo2NrMcGgzapj66fLqrRXYuep
lRl1hOy9TbaeoMPdD+wnQpO4isRDtPfHlZyAXtoSCTHViz5RxctY1JZ9AvZusaAa84raXQesk0NQ
PY0beq2pqdaUM9qxKQ+rTKmVYDr8Ged53awCyUxliho5S8XqEtMTLUZNnmjrSWR9IInqZmRZUlmX
FsKPt2h+4SsytzS3fka4LRk3K0svmiUQqkYNXM4ncS8KT5nnKagsFxX5VAkwWDpsl2GMPTXf19HQ
6bQmTTLqwGAEzDAoqUUdHwoH1YztuTnGFk2gYB0sy5iVXjHm0F71yDrfiNW7++J03n7xipxaar8N
asLM+VMsA4OrY7jYl2WQZVWxB97eDnqtVmIK1ABkGR5LIxdvwrOETV08oxddQh7fzeGxYvVA0zCT
x2XkDaJyWl0lr9RbUxvrWFq8VSp2QcDXheepuBHFUssPEIlkYwMB9xLW9woR0wg1vCvjYfpa+NbR
GXr3ekDuccDCJzt6GdG+NEmmGnfCuus4Ay2auQh2lq8uQXmZ7Xag7mqsBPRKZezS29IGvLsBjNuW
2sxh3QzCy6NxYI62PBj/trzVnwvdnOTObsTULVngSlgzP4OJOZ2Jvis7AhqIrNVvuDE4gQmr8MfN
0vKvOiYhOCrZfwU0EqFrwJCzvrJWaMPwI2I0wEwI2GEVpzh9n0q9nuUatYbVzj6P4fGAwm+oz81Z
8l7zBH9LHJKzz0G+L6KCidjMJEsY09slBkiMDpgHmn/giANhml5uSYLHMENmkkcY1W3oPaMgW1Kd
Vo9+lRjx0BuzUqXLBu+8a4aLrL5guDRfLeGJ0K1w2CigKMuOkSbnJlzf+ez0KkjwRheUSL/K4W7K
09fYBfacwvqMN5sXwg6qFRQ2MBaUNbNt+y91JZ1SudydpKmPmhyMNoqT5SKP143Xa8OMotzEdHE6
z2fZaGM0SZbjtJPPlkXnYffx2nZ5oG/tJLCbdZvml7WLw5Ftwz1bdRvqOdebXlXlcsBhCH4RRpoI
TM8kuBbbGKTq/k4V7Mpq7K0ykJpanxzsSANvqTIGzgVWwBB/uw55U6W3qfXLdZQNcBLLf4yM7bMv
gHIsIJmQ8XNMFwNwahalygbKcHTd+ZVZ+7Zba2PGFkRdZiQr1tPywrC7AxEY9SJCu5P0rVaNut0u
VhnWGlUMfpBicfxlaHtyZpbRvZ+yoEWlV04iSRhfxJ2Q4/gBGoDo9USYadoV0xfVrV+BUjSJFyI7
+TWp6dKsq2W+0uY/s/PgfAZXJO+mERqabS2Cq1LM3N1GBWbu7HnhArEjVc3YJdUymixJa+wJElWd
M08nKRGiTGtg3/QW3Zt6qjvNwz4xAe2KME7wKsnkXdH+yIbbffbEod1oO8gT7XIVq8hSBaTzFKYC
0loiTN9CRNwlMWZwlyor1nMLBOrxnYbv0IQrZfxlPTO4gLsxpXxtcPAwWGC7AknA5AEmoEsk+Thg
vpFTQ7Sj5mP6zz8H3s9SEEHDURjy+SxF8S0q/aBdLanKxYHGcYzRHlre+h394xcv+QNmK3GK/0hw
/GCaDI59/m9lzv3S30F7dQ6JyjUriXksq98VnEuonlu5jK3JrlVMrNlAV8rtolfzl9xKglVRTGCD
7mPVH23WbBGKWt3JYuAF0lm1qahRzJLzKe3MbHGqJbdqSubz0WkK2yo6TAM1BKbiEpMiarLl2s+G
Ard8XxAUXKer4MFpptadZ1PIO9O2Xi0waP5BWheYnNW0bevD5jIoHHqrlRryOuNb5oqFD2uXaOwA
pEZliUopmJW/TcGH8pYBFleCSmmD3MJ2w5F2S2NDIjizY77ScM2eecViz4rT1f2lDjFMVorTfIIj
s9l9iBO82a09wxI1b8DHnQvDPJ0K368rTPT0gmimAfuC8Dm/T4Vri55enGTpNB2c0u03UBvXgcQi
J5TyCCN6+Esgcm/1vuAZ9npddYhP8kRCTK0CBi7gwEDK3+AscYV2FNy9v7ti3rxXjdIsyweAG+7a
ZmkOv3NwPoYzbzpaXnVbhSV9aA9a+MUA3lIJRB/g8tlcdWGB/RkgF66lVR79ImDoT+uLQqLf8Spg
VPOFdRumRdxOmTq/1BZl02RENG628Gm6x0zS1WIQNZzxCt+Xnf35u6VT+4LbpD2u2h92XmTVIV8y
hDQZdayeid22mpq0Uo82BwjueAUy2NqUYooRLE9ZbXI1sqgf+SNYx5rJ8YlT+5g1AOgVc0Do1/0l
KVx2PmQEIAvV1BWrxxmTbCq8iha+f0VZDeHONR6tqPFZgOstxxcEXq/XVQBsI2Zy1VAeYA1C2PyA
zUKqcvMapp+7Z4bcl1yp9QoblyzyegM+T6FpQlvJVYbG0BPsVWYcHpctGTkoLmGnJbuQJeNxOg5Z
b+nFyaBpp8wexfLNix5aldKp0cxnaCG14zViumDmny2ie6th0Ix5Wd8sLMuIwz7sLG3pikxTKr8t
VYS8gq0kK6YtlfGsK1ZdXzOQz9GC1QNQvSbs5y4vQ1ZFfsNNcRqyxSknZilPO9COcSkVH5p5h2fY
vDUHeGZ94MwjEF75bHzI8dWEr0g+pFW7GeenbM+v238YvwQWIwg0sjpWMgJTDQipaNwz90EP2ACm
r4NicUFrXSrBVtiD5TQDEVQJq7c4mxmjHOi5BsXy+Dj72JRAY/w7+jqKuz40dKlObGtL5iMx5sF/
EBymzqQHRUUmnOpirvSbpP1DKhTADPFdHzjW8f0yuMbLgjNQl/xaot+j8Y1F7nsr/OEaBkDiLeZH
+TL9v91617Ifu6q2uKaLoPVQLJtWmVDLfBPALoVD39TbqoRl1YK9v171X1fH03et09OqJ+hsNrkI
Llpg9rJ3p57Cw729FweD7devX/x58Hx/Z8comw5U6bxlFEklcbcbyQpxeIWUkyu8ZnAeTPPRFBTV
v/p6dUyXtxk2AqRd78obu5r5vGZisO+yw/BQfglTVN0+EXOjcCivOrZyWDoqbunMqtlSKJMKZ7Cu
pYrZw9f9SHWCa1o081/T8rHoUrsOFj/VKSj9IP1SHztI9StLHUbyo+UemCuibgNqxlN7t9F0P5m2
grHUmx6KWbJndni3Hd3t/iXPOOMSDdK0ASu5Kw0Z1crP2Kmfcl5B69DztUfccXiaUzX3G+dhOI1b
78zbp+T/yWevM84KDjZ78Qs5goKPffzw4Qr/z61HjzYflfw/H97fenDr//klnjtfbSyLObt8wttR
3D4fwGHgNSCiRjc8y0bsVsJ5bBlOxFKkkDB+7HWh9oKElNhvhhjIQ9/fw6iXl4VRQBOvIM4TbA0A
ySJ9GQ43pOXhkNs2dogNa1bDdl8cBBMSA6oA1SjRwVQBcRrVCaULfyriUCXesbuzxHtvOFRzSqoE
aTH9xd4Y7AawQaU/XtiJqLNSBruzqVhInhOxc6JOn/Bm2zZ2Ob6/oK1PQ0GsPZkxJ0SKTLFski0u
5Kag1ulaSBZpo8YjZTZZnnAsWlrPk3kyLincZRnZYwQePUec4rfd0Nh9Hc5uhkaSUSrBFhAn7sYe
Iuv9QhqDg52nh7t7rx4M9l48ozv87t27/g0LNZskvhYnMtk0ydOlMFWVqNQGz+V7HeMQH+TBaJJ1
FRp1RMcpLSjn2Zb3tU1MEKhM6/UrVZo6wLYZdau2EfAlrp36seKR7+zqyLy8rVKpoY4BNvlzfZuz
pCgatML+ssMisHbpzVpLtsR0NhgdqzWgfpGA75z8MG4ZL4eylI35eilfHZMep5oujN3ylV2EIy1x
FdpK26+tJkHcL3saEDjXLMmrnT/9HSxJ7S6uXaeaQZUXstLor7iy5QbD6Rssiir1ELvyKOMpH2fj
DynHGXpWancQ2lWtniHWO6hjJnkl/PGmXOsA4qk7hLXQdid6Zq5NSXJWd2H6WQ71PmqEjWh8HWXm
Irr6yliLrjMYWRXRaX4OTJ+f0bUFQ6lSSyalq9ylunCZBjnmvdQG6LaO7O12tzygjx2q20FdvQtt
exylW4PWqax4MU+mBfYzjMxjS684J+VjgLL1h8R2sPKI2I3vst1Z9UD7N9Tf5a10FfTb66ptF7Zv
V5gWxFqUlp+/9+vt4XWut6YXKdrg8cqZDzrkwHamdrB0bIBcM4QVB9wkIjNoM0HGQKL0ksIQqhDV
0qHdNhqWIC6GbSfjYMi0MHTSORNkMr3Qg18NSWG9tDgwQKklm7F0hvwpcMuOlLQGOXyUlgJkJeeD
Gy6kX/2Ky85rrnrPVbpae83V9++G7k+Eal/jtltzq6+/1q6NB256nf0SV9kNDlq567XoONytEjZ2
H38uMrZ8ZhD+3bxV7EJtO7m6eUruqFpDwgVXF8Hrx/w047HeF/Ex1CeYyBSmXrFV0JvnWljku/29
N6/rMZh5Yq0b9ywar921WKffi96+q5a4DDqsxVe/bIe2JYaGnoWhFSXNOsc9u+SrJ2Fx7/Odw6ff
34S1/PXu7f98dvJkPntr9uTdFTduUGdBXP1k4NdMp02v9s/CI7U7Vgt/pR2joYXn2xzsv689vJL4
Wltg5eEyBZhYs0thz1PL0G31tf/LAhKrglk0Jk0OrIialcrOnawml9s9lLjnQkMJq9WxMkTbVHSc
fYSPz2w2yazj6CFbzrC4DKG7xFOFRpAdwbof/p/px2QEEePuca3fi6ijPHVr0fZixXCYNo6/WXCg
U8SmyJHTgJZwgUjMp2qhIhY8J2ovK3pMhMvrRn4IHBtBb2q0Qx2jepKQ9i4QjrPLCabX9yy9mhV5
UjuqyFPYzJ1TFEcPmIzlQHdqsNqua6vUTrmNlftT29zD6tAeekODl8DoYu247F1MLdhr0lWtgROT
E2DFgOzt5A/JIkDX8pUTdenwcuR1nKbn7WiSHMFJdxrsWmDbkks+ST4XwbFSmxL8Y00tXMNbXmjh
CSjf9NwQPdWmapKrpx9nEhDKxEqUcFcY/Sce9CWL5CWFynI6DnWR6MK3+IBhhW/jIx6iNHr8E5qD
aExqNnZyNhm9dxrJ94MxrBLpFVuMGJU+8BSqswnJVlXvqjYkn0J7j813LUkUH/3e3THCTg2QIXN0
2p1dfJ5NCQaD9hvefl0rIo455f01WNIFWdDCX/VLWyt2Ps5gRwuustnxl2qFwtuEhyyBuriauxNF
QPbJV6VfSw1+zbaNmtwMqdzX/93acKP/LdJ03EGssF849i+eK/S/3zx+eL+k/916dP9W//tFnqvj
/75M5ydMA+Wds2RKCJHDrM4RjY2OZW5DzRENwtF8/UCnove1ATTuFtF4OefcLt71YFsraqPbIrCt
xORoCLDCcKdA2Ld0lE8JOUHsRbc6jPwW85w1x9I6cZ/Rwd7rA4Lr0fxihstpjEDGHxgzN+AbkS0W
EoFW6KtZHjWHWAIdE7K3QWY0bLWJ6DONJOr8fHTR8FOVSGCVbCY4BiEiaf6vbSS8dnSancAdU/yn
e6Avt7pKYCaT+kCxqxZEmNvphRj3XORLQZaIuDpfiIwQRtgSifA8mz6Ryak9v0TRBHqcE8pkC90P
oGpnk5xd8Y9AIN7vRtterEwNvDCNfvP9zv7LnYPB93svdzawOF3RrhsCE/cHLbI6lsu4rPaaZoJw
vD+kNCwzDevs7WUolFCQTLch4Cau7yJVqabVib/ZNTbGll7m9NDelro9RijOBoerVZI+v4u1Nntq
NhxfC071yFTWOc0s1fjIErzS2cS/JvYk6nQkIRz1l03RN50DWof7Girgfeogq+Sk/kFic3Ji5oYk
yZJAh1hDLCjtKpFMtN+sSeFg0JJ9j4XF0+gkMwfsjI8oR4idph0XdqbH80/ZboHjMLJ5N0a2nJqz
DFG0xq9s2M2WZNFzCKpx3opOppYPbJ7hCa2FHxJbOXA9Ep25waFHx9ERAeJ7jPE8j8T1gUnVe5Ed
9Fi68sEmGb1X45L3UXPz8eZmq8t1/gQGrtNhRNIRQIe0+zSdTDr/tsz5YBYI8+r4NNMmXBwWY8QL
ps1iIPqQzDPgIdVHSWjZjENsaizPmuOIBDmWnMjPp2y8IqSa7mN0bGCXgQ9mLJwfO0rp+ODdmeBM
fFXf/Lvhiefmdnh2stnu3KoNDHbf9mdGI8G3aBU5cLZAotAsDI3ID4ZgnIlBgJxZbC6Jgzi2kMMF
ZmlkZRB7W/rg5pCxSxcowFOeNTVNqDaMpdT3g6Wj2aK82Lp6WWHPgURigmkjThAHYCKYesXHcpIL
ayyIrWu2WWPESq72IRZ/2DbZG2mKC7qykMHnxmYvJlNPTaDU00n68XrWMS+3X21/t/Ns8P3ONq0R
gurcIbjuiD2qw26rUBQUMpULqoUWYisoQV4e2emSfCRmlkpityAMDD68exdbmQlnHjJAwgwIwysj
DxpC07lLtagbusdMfC1fIsJ2Tb1oSLve57K0+PlMFVlDvaOGkeSqdJ+4IRtDBBhmkm7QdgIW+YDT
cR/eGXo47WiSTN8z0qOt/9EFVl4QxIiMZMJRGHAXcvzccxDwtNTTXNKhzfKJRi44SkcJFNx0Vk28
eNATROBjMMPfSOR71qMVS0KuH3BU5mBkI2guQokKr4yyr+W1Dt1VEPRUOexuMaPR8mT8sKKsL+z7
IUd9Zl/zMrM+F/92OY0AuNDTZnwnLnvpl3PbsmiwVEv3p1xXx4F/3oJv9sq9Q97yytDifmwECKh0
xUgcYGknshxNaiWQTYhihf67ajVU1yfOTQMOgQp5LAeRumIMpnv+t659zFuB/w99veP5b+LZwTDK
787Wu/AL643u0jzuxndLYwi6fLvVo7oeN565AHv+yQv4Vi6lB788YXfuXTwmrYYVNEKgeCCKuW5W
JJPp8kwD8OtqIl14VowzIjdsqnRDKLDPURnF0B3BQps46PATp7AwjqYDjlxYxlMtE7aSaQPguYFt
u23wX28VEiun/ZXReMWsOMlDeCok5kSipj/2Ci0GjI9bXjBLiWjeL83e1PKcS2q900BKrcUJFil4
ZwGGnjptHxCVAvSCrLvcxFcetNliNVDZKZgirLQ0giOBiJknMdLNdStn5gEXaJspWcfpqsFGguVM
PAu2RdWJG4LMj8HPJME5rqXp3QVspCKEfMw8S6Y7oGyWiOio0cM45SRCinOkfvZYTeCxCj7C8AoS
UN40oRQQsWrwOeHD46iucEeYKK7swqW/Qa61KhYt4AYcuvCYndfrwCzniivBPIyhZiz1q7seLAjQ
FmSw8hU9PldZjcIl16aUqr8XRJBqylS68+pbwKxt8E4seAYXBU3aDrNeGSSI3xYKrwfgqHV2fG6z
Vm1M7SY5JyD22O5/crU5vpvv+VNZ9fLBW9m6jQNuHrP9IA3/dRqLn5CtVrFwsMXNalvwQRpPWWtq
pgaAbM2vpaegAEM5QWnzPYF+S44B/QoOgLng3bq8uwmyUCfZz55H7fiZ6cTAQzr7XfR19NZupDre
rzjc7xo1w4Wjq90N7qSlr9qBt68T1Zdi5FvdZBj23lzj1497DwHJq3zxHNQnq18qremV9eXj5MeG
BxEtAi523+M5pBQcURKsyGf40XIb13NVdt7JWum6Hsp48qI7Oj3Lx02q1442c0gngrZCF+bS4tiw
jz9rX+TevOnG+CEmPQ1bXFaxxbU6tl84VW2no0RNLNmsM+ItPdfzuuc0ncz6sZPBeoJcyLaiDoQ8
DLK0ANk0Xts71eyg5k27lzhfLHlpu2zJFYHo+s6ZLALOYIV3P2bh52BB3cdX9T9JFyIPMqIAK7ax
aXyEbFIRjRVJrR9RIFL7vJEJUAbCOG2PUewHRLQxYsO61GjxFfmFRW1rbgrOPGrWAIq6jhewXN+b
G1TPRDZl7Nqs0y+aGmVNrOmiwmP6FWqSnPBSSxSC0hGe5uH+YW/8yBirjzGeIE9tzVTd9eEP0KSg
N8sVsl1+E9LbHRb7s/E/4zJEwEvnekFy5iWE0RxNEgiEjpYLXZFFLBNh57g8SrStYpQcH6OFsYTs
Zakdy22QzGfBAk5kOZuqUYobZh3hYVVFx/T3V7Tx8Tuf3KiwSvW7UNkCzWGgp+h6G+LUxgxQRCRU
wAcvUb9li1Q2ytQzS/80mUEKSQubHufKuTAz3MNPtl+wCSGLcqoqiDZFfaKtmSTNnsy6MFoGWvw5
pGdWPbGcstujIqiu4Xm9aDbr+F7wzx5Rh5jpAQ9v2XeTup1XiH9655qrGvLPkRBmldq2m2qM8y2T
zP4V58JjRQUkfQxwKh4O+rnyvoVKHD77bCXCsoBLnUSzaIlc9JMZGd3EZbcoojWdl3/BwZa1mdbK
C7vO7mEt9FrjBuFBA39/jL+uowCLch8DxdWrIoLViiG0lapRfV00LGWGsD8SPupDoEDwNJVFuQPH
ZVtQNBDiYGh9l91ul7NHIgYCbkmd1arbMmhLVw78GgOkmDqBE1ZWgiX/Xb70VDh4CS4lCOn192tc
ItEtNn7VPtbnf5bfgf3H5jf3Hz74f6JHv+qo9Pm/3P5D99/kIZ121BLkl+zjxvu/df/h1qPb/f8S
z6r958Bp3bPxL9HHevuv+5sP7z8o7f+Dx1uPb+2/vsTT6XQauJR6URkEGh5P30PWVLZDYXIKfoiS
j1Qr3dOYQ5zzlPlxFGFzMJsnLCmI5IdNC4ciYrMeIVVcrvjoMEWgRi50alJ/GzNP0cZqwrFFbRJz
JJkuZTGXtOE2IbnmCgd5U5ubXBJPJxLaYyizG0ZPX7Cl0au9QzbSl+gkTDWYuCWsomD2R/y5Yy2H
nqFcM39SS7FY0sTz5dSEbcLbiAY9k769OCn+6haR7AjcNdXmrRdtdTe7mw0xLerpkjQm2SidEiUZ
vdw9bBhrdZAZsrMqc0tOil70Vqq0MdC2pmCHyUM7+taYtrWjA6RVzxYXRi6K7BrjgSbPjgx9ejaa
tc2QxZbM+/NoeWL/OsunGfEl9u8jYx9YvGsAHht3zN4SaMj2NyTxr8KCtH6myTew6+cZG5DRtlpQ
og0UGB2aZRzqOjaasyVvIUsihicEJssjTs9aScc9bAWGROjghA7BPBtF27uRiAkbElJ1kr1Po6fL
ecHQP46eclqm6Cmc39Qi6AIWSks6FIDws+zklJjnVIzVDIDADAvghnhyJokZgy9VIbZ9ctyN7t07
AKChdH7MENO9d48PqESV4ZMoZ6ShZpYoN4fxDCZwrnY9smbLGZsqtu1rgnKBUH4hh1Xj7DTC9Lo6
m8I7y2ozBD0d5EbYQB7UaV7AWO3OnehPVOKuY1zGUMlhVLBhbPwEBjjRUDjh81N0sIAOy7To8MF1
np8aP3VWPms+XeOhpmtQkR31vsNHhDGGnqHUEJtkccyKUQvqMg604YJ8f3hIZ3a4OtVwfatB02xS
VfkYDYM0qr1oXX7iYdScJH+96FjbLmJx3dCBdYtRPkuD9u/de16DlwmMV2Lm0tj3LeasjP3evVes
lVJEjcNxkKZRvH0EgZMXCnk0yQSQ4+goneTn2vSBn9m7vC5rkQUsgxPjASamp22WgyLk0ocsiYZ1
wQSH1CtOBhItdRYZblU1pSZgAQYGZlQEaDNkUZEC8JOYlaAVpfo01/P0iIGQ7oButLsgEGMLb1wB
Q2ZvC+YxGwjxMTtNz2Af1YPULkMYQRj4JcdInpREBc2kM0aeJo5hKVi3y4aYZiWt7ZbInYhbno7a
wBhgzxepIDS5/xk1aDYQul7O0rMc2YvaXlZEg/HUJmuSnwgxwaPm4JgcyhoIGVs0wyVIeCV6bXw/
IJhqs5/dNJ0QXn6fTo3hqth2sh0eDAODrsSui4USmSFGfPFEw5qLihUhC5dwmu3Kstx/SGsDROmt
BQdSkcQ77PbXQBIU6QfxXWhWQ65pJouhCgVh6KuT5SSRFVDrWm872w21NNUej0VUy7M5ouuAUKyY
xSJcWZHB7tiY5EvUUskezkaeH5JswmafQEpBlj8Ji7ZEvvlhmHRy2OXGOLObNfKnvibpCHBXSSQ+
rMvgqQERcMU0GEasASous3ShZuiarpO9ezass4/ky1RTjgYPRGEfVwxoMoLhiGFYoNolUJDALrmG
xeD2zNYfQI/RIMA7OPheKAk3u0lKYFioYJZA+ILI5eTDhc30cPiGLu/jY5i/6wwbzaFqiJ5tH3z/
7d72/rMBlepvDsX+IeN0u3yjYexiHg6xUdvPrdQYJaPTVCCEMK2kHuNcMOUseXQc0mOMBJXndFkz
QcLwZE8Y8BPWWo+KRbsTECXwUVAURIv/Ic/GdnZIXHkmZ4YG0kCUOsKxEzsiDSNEGK9AVJIn4uqq
ARiYcuchU/eyeEITPDPJXHFP9BzJLreCQKy9KL1LljFWQO6LSmEogaj1jDHWW4Am/DPRGLDGLghM
5JTZC8gS/fbiGVavuiHbci8RPtvFZxLqj+ufLZlEMTFoi6g5hAg+mS8GCpV00IcScs+9aZg3HsYZ
WIyDGot5dnKSzgeCgfFmxEd7QLjeb+ffljQnEyCa9mABf2Eqni5G3RYt9QGfTLErhxl1MYPlFa+i
aEGZqoKOwoabwQJxopcuHJynuavX4OM5YafPNpeJhggwrN1DsTywhQtBInRc80J4Qo1M6Ld3rMg9
U/E9lELqVj0XzccZQUk2Q2pdIq4Zwlx9YM/ivb1vGqrmmKYau6prkgCLgQgB5Ux9jv8i+FIMgN26
yOQkwU5jysoAWsNdL/W1z9Lq3isGsSDQFiysZAfHwOJrG1HX03HDBL/kNMZtVqcR4rbYLrEkS3p8
TIMxaqGEd9E5DzVmsGiX9FdHMAvR/GUemzzPabGOUqLSV4C2kPw5QLxBbWrsT8NxmTtaDqwjpYaO
llL2KmpKSzQAEPo0LEKZfFKwja1SKNDCJ7bspZjUtctHzLDAJqxZY6nwZPHDEaH4+YViNE52KTAM
noTQPQcmYL8BGQFcYhpaOZoV/wZPAUL7ot8xfvUGgxXmLBG5hWmoYGDq05uNrMyhgAI1YGPmXTM/
FZ4IvBBab2jyHqXzGKnDkcwxcewEdbCzz2gJ8gSjWs/m0PXRCYYtPWEr4/pg/YpMBctVNhpMulwt
rVlUd6+A6gYHpDDsa6/R6GDK1AsWH9jlQwKzS9Eh0ixl24h1jYaMKUD+DP0QCGZL+APMD4aIfXSG
lJej+pLmm+yRDIAQVu0AasFAfAKYPiB4EligjgENhcmOxtKeDpyU2CODUW/E2Jg+w0twTnQGLxJg
Q3eQ78qIrj+JHasOQEIV53Ubx4YUQFjCSF+YYhzYi8ti4+hiUEeWoVxif/uP/2293vgQYncQXEJE
YbEsVM98p7acO74umB0Y0Mrkwm2rJjsFOMyZQJEpCtJVtKRnADKRyHknSqx7dqNQvGPJR5rh+RRw
bwghoGR7lOaReidTa9qDOZBwpRJU9D2hAXOLc/AO4lQbDROrmF1M1HtISvBK8EX1e6Ei/jD4Pb78
gUZzYCUdFsQbHojHcskxvBoqs9kaxm0np9LDNZnQCRgOh43yheiq8ecGUSS03JIyR8ybjUBocTFL
lSDBHOxJtZss+4FozHLs5ixPYjhgCPHEt3R5f7s0foSpCxlcOscj4inoRLQboEB4SGeJEWgeJSqH
4g4gMSbiJpgSyBL/iBJWdSebvq0lTjBjn3r4PXqgLeEtPlikM5OnaMbmpjWy50IxGKBJdMQXK+R/
dIhPpmpcAUNmueTBpy2IBqJZ0wo3IK2765lO4OxCZNhEEgHf2QknSD+2GO3du8cJr5czTyoURxsR
J2CmSfz7RnfEUkIW2PylyKf8VbK4R8guQaclvnePgP5v//N/8Zo7J7zII+A96RGTSRXqy+4MNcVk
TRtQhFmN2d6Gx7qrgkcnUOHhHM3TcyuU1FwR9J44w/kE+nTzrVucDu1Y9ToM1icQmndZEgPQM/Fe
iML5YIayTxjOQ/vZdBixe/iigjjc8rxSr02J2kOAjvUSXnUVkSNuh4SHJ8lfs8lFw/MoVp9M8P4i
rwnEdb7zog75NR0/n5h2Aztgb2E7rKc0f1xi3h7hsBgC2lHKQ2HlnohN01S5YJ/MLjVTJrRZzjAN
ikj9oA/h96gpUNobFWKc+D6PlhZynNGHpcBdj6DMqSFLksuh/f+WWBrCHdkxvDIQtKjBFIHeCpM8
J8RFfOX7dCoRkST0+2kK/1/x+1ws0rn6gaa8y9hc7via6HVX5ONqhlFoLqq2OVRWAp5ojjTBqUwc
gbhwvEkB4p5PWeZae7i5xQnK85QF3nL3GGLNsqUi34SJy6Td8IFsnI1RDcxoPazCb4J4x+VMFGM+
0dAgHIGhLFX254UHIGJTDVr0SD+zFmKY5r17+8KSNnQcVD6f+rQ+5OxLds83EntxoeZ7WrgqxqNA
ds+YI2V/TUshInIQToDBu5ZrFbisKH2GnGoOcNXR/aOGt0UcZyisjBkJYl8S+LWXmj9angypyvdE
sSxON/T+cSxLERRX5RMq4PDa2Fp8exq2TW7fJkSpH+RUwIiMSPo5aEC5sdAoS4HUYxXHUvugeh3/
WjRTJqrxhH1GO9Kk/xFNd/6SH/nvAHXZqFNki6AdWrYPtHd1XaBd5Poq9K6rnSVkh2IY6C+MvZIB
0+bl+/RCPHipoVfpAs2jrQ0CetgnFfiVgVXYgKXnxOwNY3i6wTu4xsd2KC+zk7kEMYCEjMAtf78M
N4cLpB0gZKih6Tt6jkHkEReyi//8i0PXhs6Fb0zMDUGGBklSwF6xZ5XhnkDRg80el2lrr8HCUVvN
4irVAd9gLT4poOYSj0rde2U0so09eJ4tjxaTdHHRW6HYhjJ5wb1aqX4ozGfyvFHhz2Lg0sjoy7V4
bOVhU+ZuIAlpixJSCc4GjHnFex4UnjlqSiJ4rG82ps6fZ8K8oNVrYt+//cf/4d3B6IyA0fTCFKSO
BrI7ZT4vVLfLgn5P39FomtRR0VCU2MOW4Y+K5ckJxCoaXk/YK9Y1KHeWyHwRTYUxHw4rzUR06sxI
Mc+lqaj/kaM53fyxWLIjeUV+DVPAm9t/bT14tHlr//Ulnur+K4ZDNH+bKufnAcXN9//Rw62t2/3/
Es81998W+xxIuPn+f/Pg4e35/yLPjfdfRPU3AoMb7//9zce35//LPJ+7/0QNph+7fymu08d6+99H
D+9/U97/+/cf39r/fpFn415Ut+EsZa7knJO97zaie/S/aFvCMMZPJXcRDLVs/Zio7rmNwrfIZ2Kq
mEYvNesIgl9ziL9peo7GJKHJRm32PL9lFs4cpeVgWc6GKLrn64Ygme2k44yZTk+YZ+bwekKsV7S7
+3yHZzzNNSI3jHHBLyQjCUfwBiOA5UL2gVk2Yl5s3695cdDawbMfouY5HY38vDsYqNHH6xdvvtt9
NaBvg0HrCYt7Atl7vMOZa5VFRzNWYBMTlzoqutHB4oJZ66P0Ip+KZVk+jQ6TbILOouUCBpNZWrDM
BNH80IoeVE763R0VRdQMdlEiZXEcOI5oqQYfbKpqW4ZoC21ByI0NgZHRaJIUhQmcxzI7FXyxetcs
7Kt8kfbsJBH4k2bCWj8E9l9sSOiFcERPhOcTb8RCeo51GrEJqie2YBywjwY1JmCZaRLJPJ9YE9QZ
TUc26izX1aBt7UgQQqTGTUeLnJhesKES9BHmDtkxMYg01pjTjMRip01LR4PN0smYB8T5uTS6Z3Rv
o9E8Xk5FlN1sRZ8aURQvNTrKaBE/gRfYB1obQEY/WgcaTxrs7db8iv5qqYjxSbSx4Z1CE5Xh4km0
PZu54H04DgXLZbCmy8J0Cm9Uaq3LYNwVk5GdCYcMf6JFaKxiVislTyGU7ZqXXqEdMT8olZK3XjFI
uTmAY6mgeW+KPtUCgKl8Crsh+wUT7UdPu/jhv/xesoqZT/KnX+AwW0xS+53/8j8/zTmbki2gf5si
3ybjE6nNv+zr5WKRT+U9/zQfdqezpTTGv8zrFxyeHa/5l3kthjf8Xn6GH/ZYS+d9lhemECef+H8P
9l7pqtm/TQGRiGzPMi1AqNxCnsAYQsnU4fnYNAGp87fbBzsouEFFNgw5EEdfmza+pk+laNuFA3G0
gNzcB/DdZkeBT+Jn2YviWAPX009YkEXNcbrAgjAOfbP/ohVHl+1SHdinDQAgk5T1c14bclmoBZtX
otpIYu6TAS0Qorn7zdjLJrIfpYV3PCk6e3/73/9B/+MYB3wp8p//WP/DTCyKoo3lkCMvZb5N+AMz
0pItPCvYzXs+twYvdIVKjJI/4nVX1ynq0ZWEYLLcwBOuT6vl4BQoHCaN8e8LVib8oRf9/igfX/wh
fhI9T4oFbnT6G1cW0QkI0h4RRNCt07VjQXF4kRcnxo6vaMY9hJ/6I78s4OTSlM9Eiu4d69evo/st
Gh99eGJCy+gMpV2OfgAUg4FKiIkm+tJpCBLWQv/0T6wIyDWk5rgrg4z68N8teAVig6vDIn5j2/N5
ctHNCv5Xm26hbfmJCIHljhDPijcj6MhMIwq71MKmS4lDdglt/ug0aqau3oZc5LxDoHaOxeFkni9P
TnGVudravrZ6GcCQxD3J/pr+kF6YaIif/FoKGuJj/dNPHFmQXp016Z/8BfJpPU1o0Vs2WM7GvxZf
b5y0EcajVe0vK14SjWZMvCUbXZtj2Jh3pn++PsMPOiRO2vXEwhYn9QJweWXDsT2xDXJZB2NqGR0j
ZUQ/2qzrgU7CK7NGqjNQ3cPYir0jKAD5m5r6C6UltaXPEQ2EqDFRsUK3cXoxO02nRYv92LOC1u6C
9Suu/UKUUe4QadyjvsxYz4ydQ3eSTk8Wp3Z3Vu2H3VnTHM08gALJygbXf+z3+lIYsdtlh2MtgU80
zFmESAl0lEG5t/4z0W4AiQZUntMImzTeWeEjTw45mODy509d8zetyHQ5mTAtZyMY8FqI1kO5E9uO
4Vb60VdfaRtPGm5HlVozNFrTHG5x4jSd/tH84vWO+ArWgkcEV2/mk7qy+MSJuPzydGf8gGiI7g1t
9M70Q10DJvVjqT6YvrriNsGgX54t/OtKS2IPv2iRLrYZO/Tk/NkpLouL0quU46TxTsiry5bDBwxy
fVlbwqbuA3Xw3P+29c7finQhG2GoW5M5yXEEnHPER9zaoMc0ICOnX0LankqgmE+XT7wPoO6b+Ppe
A2JQRRR8+/4dQ136gX7VVphJuFUeDNeYvTOpYuinX0VPOgq515f2crw06/lWZ6IpAFveunAIWoHS
Mm9ksbQBcrr4vsLqM5gqHrI4NUYoF988PTbDcBc5QbJ56d/0kftMw4BFAlGZTe5J4d90ZmdWc2G6
sHbfUiU0YX1rEplmBOe7ZtHq0eEmlIX0XGJhmLDLYxxeyj5xgSEQoljko3zCd0mMpnqxIwzqSxS9
OkqgOkCUhSaf69SOg1dDzrIuBm/Hxv94u93570nnr4N3+mOz88+Dd58221v3v7n8zUYX/iE1lVt1
wyLMIcZUybyydH78F+vzAziojNYAJdCoHF7/GC6PkNm6TzwwHK4cyKUhzKWYXdrVDLjqctKqvGm2
AiCb5zSqM4nQzFDdDAlF+V464s1PBt+Y+u4EmdmU5yggfcFGNn2vOcHr5TPi4rEZnK1lQuj2rvSN
r3+zwVGeXVWDfrWqImr33SS7jaqb7QopuuYi/LtU4vJJGeTkQmmZ2XZdhlvvs7ZiKtPx+paDvXN4
b7k0OQ44+8xD3sIZRPC1ycbZJurwnAVii1bXZHmU7ZEbYiHxJN3V4O1TiE2wO9QcIkwm50m2cLxO
03DO7QCJwxgqH9Nl9Xrv4DAIn6f51nvEpcYqhOgcEvEP9oXNBcVabkMsQy/9quBTesK4CFOQHYOh
4GX0tsQHNj54yrX1A1pDnj9G8Rv2Pho7CuwueH7MVogHYvvvdv3oVj22Zb2ihshTCfEal0/jOxXZ
sqb6gAhYjs0Zu3GXbwkGDXvf2zc+xIW3qGyTlYygLsuet22OkKZfPDLOZzGYglK8QzPQXnXcYcGr
DkN5dyK7N4hgG/HUhMwBcQHTHlqXSmtYKxyCyqL7C3gpYZqvXsevrlhIN8JgqnEEK+fEODnqsJss
gEyjuwhHkBR3xeFTvkEjwIbenBYnW6j1nJlyMHh3jTMtnU+f5VPi9mUohMby93HNze2kGbyh3kkX
WtAc9VoxiEfkBHeMXj6nhrqLsV4WQFx3LBd/xdg6nkPIFpSDoe0B31Q9vbHcl2n+o14uipLKxNap
y8kZj7MPXqOfarqFqDpAHKdNFkYSgiJq4GzyHEsgZfm2paKW6fijEF0xzjhTX/f8C+O0ycLOENNl
46A1H9RVCmcvL/8bcbDsFt0zffsfvcCT1HjaPekS3JywDTCx/hcdTnDVmUzOgu7Oko8vmKPtRY8f
+h/y6VO2DeuVqAMFEbli6Wrl8K5dEVwQOPhreGmXofWr7wqScGNTHFF3k11A7VWboNTB6qU2sSSo
oW76MYFolWMdfNiKb7CglsX8+1lTogxkTTWexA0WFKX9yUNSR9+Qnfg8n49XLrYQMivXunoX8238
Av7lmtmGcKWlcEbLOYJg89iDOiwP1+Q6uHMnXgMaxnEiUXbHrLsqIptGSinzYAaQzz8VmTpmmR8f
32TnjbDg72fjEVyYN95jRW6w+ai+aoOFIr4Cb7388+D1/t6Pu577yk0W1MharrugmFBczBJEpa9Z
uVMCAFmOHVmKwqT0Wplp0KQ7Uk/24gmV5mRAHIKWrfRNPAxfwR5/wT3m3Oj2dEumdL+et9yiX/M3
IKQC3c5zMyF5VzrdIbckm8lZqKo7+iE4IiIP+1DexSj8wyrUumfJzJMYnbVKY7YUiq88xDJxFpMz
AZm2Gf2ZASG6SbusCgsIUh+UvvAW6q33zKcob3BUpfyqw8pfV5/VAIXywa1EL1lFa9zf3LzBgVYW
4cYI8qvyffHHELArG3LtTSntqzbGkFGJ+17TIHtsHeUfCeGU9xScVLmnFdsoj7eZVS4Mj969rs9K
Cf4EsjLkdKoFr9gnT7Zs90rbrp5bPJet6kQNJmaxc8R+oUnh8TzIO5WyFVXARrXiUlvBny4GuS/P
jmTCzOEEMCJgUbdzopuNvN8dqs6H0LW1qrcbYQLJKFCUcIE3JzGuCNCy2esj/hQjheVf8XdxBofr
JadFGhsZDm3mJBu971l+8Sm017Tmjs0QApi4wXDrYikZ12C9q8cYbJKOV7i7EmR6Yw/e1wxv5X1g
S5RJxgN2ivvbf/yfMmFYR2KaGqnxlynHLS+fnMrB+aO4Mv8TUw4KzuVGjJDIik7q1rdldQertYHz
/Pwfw+yiVlO4n59XFYWi+zNqQv7LaZxE201f69XeUifQca9VDgo+obJnSD6nUhB+96MzzfFEJIFa
bJmtUoq9yQKVmPnyljEGQv0vWL7yzh8LkIfXOA/ioDxg0RGfiZHoTz9Fb995VWyMMjUi7XuNlEe4
va5sMGbp7DnkufCwNnJC8yaYw1df+QMcHEsRswXWIq5iE4inOqjVs40qw1jdteAJWnujhG9Hfsnw
L1OvXTOcmrn7wDVPOW6bXc+yuiWYKW+/v+X1AnVuka1xVknV2egMgk5OypS+2d99amwGfbMDlJIZ
xh41W7Pkts/6Za9deFcnXHU8LF+tFIBc1b0Uk6CWW5fKp2uIMqVmneRyhdxSD4Kikxo1dXkDhfQI
e32TfSFVNT7K7dznK/0GOulVAFcGuV9LJ2DtaRhKzSEMCivRX+FBQl2AoV3EAb8Z16sD5IWnAnB2
NaIDKInHr7eTa/byM3Yz3E/PNir47N1NV5X6UfPkxnFYohY0wnVddaaue6r+LtfvOvM2WMGabFRu
CJbJl8CuxqBjnp7lH9K1KJ/RoJq3sxRoftaM96VeqCUMofWumAB7gqM/xtYAxLsL167+ipW/4aqv
xkA1i33pX9RXXnY/73JboVh+tvNi53DHw0r+5pdRiWxFWWNb2osK6vh519F/3gVSOTBXWTVVLk5a
d+MRIIt0mhQDNVTQSsQGaWoigV7DAvXcJQDrCY8XjyF+Dm6JoADt6TRn+X6JSjYjKdG/ar5ph9MM
KFn0Jy+oXbTtIkepnfTa5nqmdrSc2oLxKpWoLwCoYf/neb6IP1OdSQwglqypjBHNKjLvVcAEJqa1
UrJQkYrV99HJpsd5SVj1WfI1tLWA80mNjK1eMI+Aq53ijEB+uuicEc++NL9zggdCRVNktRY1rTOh
DdvO6lh1SH8YdtDjgtAKJBApwQHCg9IwKoN4+8+zj+9ECMtLW85uHEqAzKyu2wUvzxEKcxfmiLXK
7ZU6leZLL2/YWam2LKO1PoWNOtRepc2tDOwzJ+rOcKlFjzH7GTtX6i1WWtPhGaHrr9zK1jrQF8C9
DuzDfJiB6WPhQzHenCE1AMwiUomCFMB2zcoY07YVo1wlp7vuia8TSkbRVzVHqSJqx1MVB2pvlTdR
WZRZU2KVgFCekrizpoQTJC6zihjRNmOkpHUkXPiwxaQHPoaVDF4+qa0q6eGvQTG7Zw3t7J7PoKLd
U8NwlNnM+uKG86i/LQk0Sh/gztOr8CnuWUG5u+eydlkrOKz2la9hqABSiAKqCKAE43XwXd6/9XC9
GqbXw/OVsGzhOJBJhaVKq0MUMBeNVEp0Xcz3j7AKhtjfGWeL9YuAEv8Vpl7F58zfVLtxgILvV0FI
pY21unAam4dQPDr/s+hGboqo0NlyUbkO11LqK/uVp2rwYAdRi35UZ24mx+ivrvY6YwffKbD63Oxq
uObl8DOvhzqMz9NYXeFKRL4Klddi7hUvyztfsgTBy93xqsW7wi5EKju7EPnbUqy74/qLqKLmLt8s
uFvW6PivD2KhhQavQLT7rH0d8wzzrDbTMM8qM4DaFf37pWoM0IZ2JV+ACLmSfaslk6tLdhWJvI5A
rtwXYOm+CuCrxhBfnnqa2JK7dY3UWn+UX8SwgK+aJF3TliNVp/vPNN8Qy42gzTotO+QyEqjnP119
/vm69u1ZZnThBQJ7NH1d+1sbqIK1m7ZgoNUNlMxvfeV628CBeRHUi2O/ngae5yov5HdQGoROufyO
1Ze/MH+t05m/hWUGgl+0jSMp/ghqsOjTr2Ks61loK7/XdiGQxMVVIVBT2hZXdzVfu7hWL6wL4y9G
FEz/ap3xWiesUPhst7sZ+O3UK319awsuzunZrcBcAhlcQzLuprJeWRsdI3PH5KJaGwvk76MVULdl
1PzHemMD7IvxnkM1/B1o841qoLR5ri1IbNrR+2w6DjZQFQqfWKLTi1ypHv+Xlyl/H3sXyNVDQTSX
8XiFu7AD2xA03FjKr8258IHsstTfjvgPelc+xBq1vcqXn98tzi68mIJurUdTZaFtcyEkrFoOq+oJ
WwyHUZbbA12aG2eFbZ+LtbRKSj876jxYbdh3hTDueJJ+jLJFelZ0Rimy73Ayruz4onOULs7TdBqd
JLPOgwjlOud01f1MEf1ZNu2cdzZrZPM2aNQK8bwIV2tj3V1FAsWzK+xojby0Xkp6tuhs1QixNQpS
B+4ctWHzrMNHNxJ3zUXuK1o5uplL+0gfA2/NpIgqoyZqZMRW8XXjf7u1ZbUHahEhWVzK6xOxkcIa
migQx5orLyjP8nEhLK8yFDXkncEy7CXgGyauF4utNYH34ohdocNCjslF6ZzUzKyGh/eDjlxNRWus
jFqnOnnElbJn0VEtRcwmsVeLiVejqVKpWpTlnquoaX+LyhukZFdpEa+imgVM9bJ1Rxk2tFd1thNY
WP8MG2vb2Loepc56EKnBenWw4cZWs0nBaFnhKr+7fKtzHCoe9x+DkjKXcK4dWBBcyR5p60w/XHuv
vzLJneC2bJePI2aYHTRyeYx407o3X33M6tdwHfiUG3hF2FVuB0dnXnD6WsI2uPc5UYRN34VUrR0O
/aS5eC2lmRUcrXIhKQg5gSqSzjG+Pkum9FPiXXK+Cs6dy4FcmFvJJUWDZB4yQTk1JyvCIUwXFdR7
vfX2UbK/8t57twehlKpEWsljSRHPVLoqLWJBlbWWAHHpGS6VyvIXLV3+5vN0veCvqsQYuKosDTAk
oyEGq9tv6K+e/VUSNq9wsqqzgTcp401KErEIipqnGV3jU3bKAxRxfmPO1MkXuAQ1RTjWhTSzEUR6
bNkkR0EI3gnydCOOomYLTUaLZTIhzoqmwCFOVwQtPRgMuibJUVNetSusOM/q6voHNAjbhjFaWeRE
NdW1eNkCN/GfHTf5v8rzufG/bVjha/SxPv735sNvth6X438//ObBbfzvL/Gsi/+t0Zl5qwsXMTsI
Ii3JO1eEj54idyQiXhfVqNFoS+8oas6Ek+9GL5KLfLnojOZEMY4IE82XEw1szXcZpzA64/RAErdS
Y2ZbAL1bRMVpMh5NlRWKnh4c2PBSRdTs0PtJPu/ca0edzpyuN6JY7rU0ujRaYnnocY4EzRYlasLT
M4353Og6sh5XlkZj7DGbCpzH7KogZLpDkHB1sjxje1ZiZ3vR72ZU6tI1c17fSjLJTqYd5o7lZYc9
sPHJcMkj4T56EifSMM22o637M25pJglY6cXm7KN9e5TP+T4kvo3mj5BctE52geRrO5qfHCXNrfu/
a0fuP5vd+49ElCWldCF72oAu69m4jalKOZv7rBf0wtwukh4nU5oCIhy0wqVhIz9eH+2K64WNzFnA
WTd4v91yw7D444axtLQKGCbLB7Ix9EWbTz5jZ++Xd1ZsAa+xvyL+CEEkcnKPXoT/VocY9AXbq6An
TnVeNy+iROfIE9eLhKjAO+bi3Yd0MslmRcaRuc9PM+SbA4wRjZjLUIKu1Zjq5hN9vHKiFRjnMoSs
vK7Z7k02EbZmwvn/szTJ82EAAAvQ42yaeD9JkVeSZ8NnQoKUeo1yOMhgIifzjIOR49+OyUnWkb0v
YF1AeGzRhA1h5zhbtLHcZ8nH5v37dNzoyByr3HfFmfyHOY7sO/5ZGO+hTHEF7Bp37s8/Jm7zzWpK
jt2exOuXGrriHSIt6aSiHEIic/MStBHY/gEBZMRmGIVybMRREc6vDpYLvYXwqe8c0t/xHJIRxrka
Vd353e9+5y+sf3oYxmT5aDbRRtTZqsVEK5ZlzampvX08u5PPXP7aplyD0M/rnj/AefAqqLjh04od
rK5eWTaKlO1EFWQBlFoxBDdc00qxpO0pivY1m65pGyHSVzQ+TovFfMkkA+3z8dH90YPHfguIwFKZ
8dZnzZjQOHAYEkpudn+HPm5O/92Y/jdpMjhz9fX6uIL+33xMxH6J/iem4Jb+/xIPIDHmwG29FRki
IKDQaCA9CbDzOvzmJZqP1ev/KtVEoD9xkotqOp+o6UkrWtfK7CODykYymh/Si32cG3lLxE2hw9zq
bnU35e0iOaI3InuKaYin+L5RXQX6KtRSLGaLDZVrxpCiFPTyrRTyJBj0gh3qNKNMz+azMQyPjAA5
Ztw3y1vLRxoIPsr5hBNSd3YRf9Zhr3lufP6DYVyvjyvyf32z9aiS/2vz0dbt+f8Sz52vNpbFfOMo
m27AO212sTjNpw8acRx/q2F0cVbp6lmkhU3iOxzWAclwWMkY1m00DoPjTIVx9UbZmSQI4YRPSOnk
ZTuXnMvSD3VAzQrdmU0bw2Fw/QyHItU8oxO+EGY9/TjLkbqDhogxz6k250inqkFam1ooHw5pwDtG
bUpn77udQ5zoStabQPSL3MdV0b/kpf6gNmDR7rN17W18ep9eXGoEA2PVzkoDU+RuETSEqMJXDgw5
FDggKwf4paUqj5IaEi/SVSOywnp25K3Wb/xJ9AtzZCiPJjRnavJpeSl4GYiJHw49PE1rTSUlpqFm
wp6nkzThvF8EAkRRnyXRh637X4PkQQIYk8R8lk2ntMOsFGlJ5GcWzqALjmuYYhTDoR1Aj4DgLJnN
mPQmVpagjY1K9dqQtGQJhscjH/gVG7K/SBFGkKoSsWRynlwUZtospU+Iu2R5ValfCfZCC9NmNjrl
cLiaMZ440ZNkdCEAJAU5QQRRprJliFzNVqEwhLUiKiNrQ1K1dLy0EaONuG05P+acIQkOFBQDWiTt
Rt/mxOWAvU0YvDlXOe5TztllTvdEclSwKA9B/ziy9myedmgz7JIhEzxSrtGBOUiJ1yIqlBp8Y5La
EaRmRylyuU8wKcnfE5XO4D0c7cqVfxcjUzMEYtXeY8mWNOwzunon6TnGLDo0UVqA1ZotWANyzIEt
J3k+w3zAy5g225zynZGN6NI0MHhIa8yWR7RQEw4wLuEQsQbUTsoJ0o09RUSkUH5OSyOsYKF5zXnA
kQzYpHEnHHCcZHSkn77YO9h5BsB+tPmgBe8qyYuIwpJdCiCkydwEOaZjJDjXpG98/njK3Cf0Modu
q9zQWCckcCAaNqiez03ERo19WrQ1bx8fE8+zGfsxSU4Ua8+Wc6thNBmwmsPhvUHpmAwYdKkuT3ee
0WCKVrRAkGcgBJOriDMtjrMRIWqB5AuTy3E5zRYdZBVAInhNWo/lkUAdEUxO4ArYkHuByhUb+O8g
IEVaT7iOyS6lN9YJrKIZOSxyugKkxcFoknUVFUF+PZDfNIMNWo8i+ZDaN13chA0G0sHgeAmd2GCg
GxTxhvHRKxoNfYdryfzOC/Nrnppfy/mEToaGaQvfIcsFTUt603ecCSJy5fjvhhQ5pqnS3M1XmvU+
X3nt6PvDw9c7H3Eu2OB/XxtuyJUY9V3ZZqvReLn9L4NX2y93Bi92kCfu8UN+88POn92LnVc/Dn7c
3h/sIz/cnEMCIxdkcx6vTxYRtxp+Wrgmok3VJXary9PWaqDai8HzncOn3w8Od1/u7L05pEYedTd5
gM92D57u/bizv/OM23+BDh5tbjYajTtR55d7qLXXOAj2BExzA5hKxLQYH1kgZlTEYc8dyP/CQ2o0
xumxvWBxcgeAiAH4uKZEsyY83Io6f8C/Yu5BcPwsnasiI9JhWpsDaNcZqBIjAOKo2Jo14Qc6f0cd
SUhlL69zYKMgk+mHra2//c//5W4JQtkncz4f3MpJOuUboeCbxdy2vd9T33+gs+dlseKgx7hh0E8y
mudEMQpBUEhuq/0kgzKJjfLFEoGvVoyGo1kgmxZjzgumHBO64Xm+1FXXrEZDDQwEoovlEUHz2/8h
EPzu65jTX7WxfryiYGWRyAx5F2bNVpcTajVb5gVSZXGDQl7EnU6Mixv2C1btL31hCCYrB4pJmq1G
WKDUKF0XmEzQ2hxL4K1AM7abyePlbCsQAgIGaadAWi2YpBT5NyY0zk6yhetjkk6bHE3rD5GHAa7f
JUwI8pwuYGSrawjhyLgfQTMEZm1CsIFx7m5y7pI6cDVB8flesRUjz1LGXntv9l90q0DRNbv8QZ1j
sJnQkYR76S+xuCatmbFNkeWnJZImbGZBg6fVEc60rwkCGZAley3tTJNDrgMKJFFPC4PDNy09TReT
fHStEdn8OlM/KxHCx/sZiYKN4QF25wpuG/SxvE0mSEJTEsVU9slfWg6mUH9OzBpIcZ27vRrWTM9+
YaCwIRvMXAHP+XEvKt8p7ah6o0RhkMoYrFF6NltcCOEI5c2Y05K2XMHqalVWSMO4NNWrrbJCfDRk
gWT2K6APxQTSF5G7b7uMbxkFtdZBgcm8dv0kS8HU8EZnNjjOCCAGGC+Rjj2+v9rRPdCNNbPTCPOK
7Ao3RAMY1Eb3JF0wYrEfab5ZYXKMNjU+NbeNFRCo1PUJjRUDuPVX0CScis0svIkPuPiai3EfPJCH
V4p0RO1ZWtQjp3EhZQuT1/xonp8TJ6G3pLAMtBJ/SUHk1q184Wz6VBJiGFITkJ8wM7OvMOVVvvhu
lJ9Pg7DsbLoFdhTiTCabmbvuUpdKhp/hAlduQm7f4TBHsFFuhIUii2wiFyY8FDW4ezeKDpYzUDWc
bhzcIpK1QERbjKAaluBHR+lpYggJgC2SWSZFkM+SFnBMQ0UGP2o7n3zAMulyl49Fzf3q4+RpYLLq
NtqHMjc3BjYJbFbTXhm/+wcbL2BCaL8yQVRhGQy9Tf0AyAaKFUpNYl5BCTnCddNMmVKPLMFeN1cF
akvyKcPmTmgNTPOq4zZw8fojOa0DyYOApROeGTyizk4MLM0uadV+CSu0BRVzZgwOg+WICKlRmYO8
9o9qzQmtdKJjjV1//GfL3FMDF9SK74XJIBsXpUUBj/yWmnznLY1ktzXyNOKaOa26MZEVQZLFBrUL
Y3vruQ6Q+PqdxYqyvrjiK9NSt1CoTjjq98CEuvewnb0sLUY016brOkCN9nUXZlXTsaE+uJBbJw8l
xyZiqt07h5NdhbYsZQmzw3HMlYF1bmHAebK6JSzV+pa0iSKtKaUdBIsrH4Nlq94spQn4iMPbGy0c
Z2NsjSQzMnv1WVdXzZvP2UJTObjobFFzEuQF0YGDEXXLhHPRdMkBzbVXPQ3fLrPJ2JganyG3nhUm
TSFH3/iwBbmQegiCnhRFgL0S7JmwNNFYEbslTUtEoU9w6poyorf1K+hDz5WbGs6a158X//eda9J8
NlRoF6Ip3OrU9Yet2AOJZEIX71SCUbt6b3udB+/80fr91EBqfStc6cNWUI+G5worULjJuRbdOwMS
tloADq5cCA8EICIagEcr0cWpSRfYi/Ij0CkhUEQ/Ra9yg71pW3c+LubJaMFIkm9DVey2PbVum+FE
YYeQ5seLyHRWuHskODU6hsrBrC0lqIrmlhCCLGOQAD9ohbdS+J0tVMZIQcMGB16z6QDIqhDgbQkW
shYh2tmvwoZaoAbA/Kb9k1PGSEWl8fKgrr6/YPFzNYZFKcF/9agMkeypSEDw1C2KtFODqL2GPFwt
xa9A1dWt8RqLY39G9oOoE/WPG2Jr8yK4LCBUsSVb0R/6Ua3wMmz6iOiR9+vQPXu+VwTxApzCMzve
jf+8J/+ANieWBtZdeQIP6BoZa7uxHilwgFGPemLEAEa8pB28ywofGRNrwuRaUW5pOESj0MylyVR1
t+biGeXLydjwL+zhQlg0Nymfn4CmFcYdYxRw5kayhaIeeLuqldfxEj72UP8kY7qtFhlkNNNc44AR
w8M6McFrSipZMxUAg0V00UsjR4A6CryNqt2gkSqEa4LOBooZ3ToWu4NRO0vGqnOad4pszLo8X/Za
qM5GXKCVrTQNiPdfE4Ohai6NG6uDxqIhSqfM2D/d2z8Q1ZxcP5OLVsh1VfEFU6Slc1dGFvYer+ED
zDccQP0HBivBxW7JkJU9uBzAtTxOyxZi6c8KdkQydkWxWK/SdsSh/KkqoDMDk++Qjylbqkm6u/aV
UjAlcRYmZ0eFDXLyKtdYn0fXtQIpaE8c8qGGzSh8SqOOZLEt6Or69Itbmjpdihw5zTwcpJeOt1l/
Wpt82HmwIZjNvLN9QvCJksITd9SGwmxYx1KEGyZb5KUldnR/2efcjrV+sG5WOuC38ceOScSISyqM
w+wVs411jKEXisf3N+8/6Gw+7mxuGaLNjaims2063vk8+yuvBLdwHH+b0imfR5+01qWuKO5JFgZP
ryDAva0KRAw+nuiXFIFd1dtBZN02w+vrv6HXOgt4S7XpT7gtNvXvtsH9ff23BZGNodSqLsEuFzlg
gfWkEkYFxbvglwk4xymUs814uTju/C5utUrzEj7/CkrUu69FENL0daRd4jc0Sk7wmlWc8v5Q5qN/
OaFoO9o7EP/qcHZQimRTFfh4/dYIYPDcibY931xImCGxENRfnNqbipMal90rofwvNQaQEXuCHOls
bdPd6JAWdREtp+nHGRHmhKjMSm1YhFZqi5eCQzUgtbaL0+14M3ensq3GymUICEthIJyEmyUfgTC4
LZ65hK6YkoJCbzkfedJVlCvTDfvpeDlKa6RNUTPNeC1Ex9cy4tXt17t3Cy1SnCaz1LITv/6FpPdA
pXURnQNrN1WedhOppeMJMKmBVfgxcbNanRuweh7uBkLsubY8fK0WvaGftFuaXu2d45W012nv2let
V1sI8ZqqKwVfpbqwhl0p2/NLG4lgTV+rZIdebc/0BUuS55OqrM58b/kVBdypjvwwl52eHGBFKLoH
QMsDcxbNNDTtkzlL4bESut7E7ceI7HGy52h7sSCKuGRcqGqEOfIJTZ1JNEv8WMQJezAjxOmWyMGg
1zLM8mj5pZmJL0I07wYmri6hMU/n6RGapmSZW/RaNT/L2kOnLOUoI8zz5fjvpjvWthkit58nAefH
MzCsu8mAxRc7F1w9YSyxTgxUwDqeyxKpYPm8apnYIThEWBkQD8Srco1hH86XRrRw03kGcxR4BRNV
a9OVpcpLCizVsJNVcDWd+Uwkqjl54/ZkUmMtK0YerHFSS4+2Z91IlLgQXIfrbC3VxhMRB4onsASs
mFdmsrrF+4w49rGxx3QNSilWuYrx5RFdTqJMspQccXh/Ok2NUs1fARoCTCA4IaVc8bLaft4VaO3m
+RH4U8cOymI6K9dqyu6AE60P8hTwebp9PW8DnHSnSNNpD2F53i5ofVMw+Xwo36EMsp+q0sBtj5HQ
y5n3Il/VCfdcxMUSW6m64raaJHoBQ9i7rKzrvR6vap4KVWdPBK7xGmKGaZg+EL0k2RFU3q/MzhuO
AaLScb7eEK51K/DahNdA3/+jVWk7c6JhCU4SlMBWd5PxuNlU5MCEwTuDWNsGZ1jKwH5qKRDoIQoh
oGwoXQsIUrMiiQQUKABIiS+862afV8zhZ263zZtwk6XGWvCh/HsFqfLVkVk9w0hM+AdBsMymf2GE
mmmh5EbHJyWI8mW3VRCwtVaJqhTtWTTbdx2FOCs0e8HpN18sFb8Cn6mWj7NbWI28vS9p9kvaVyIo
sRLBAjhmiZcCt6S9DHGTyzXE47UhSiOD99VqvXRh6m2IbGEJcYnRsjDOEc43wAC/cKpNhjUx4WQ/
/uJv//N/nV7MqOtCKdsid0516dha+rOY0Rpnism8ZyLK4k+FO8dkxtUNWQcpwb7bSRoRGJumqBDM
5FqsgoCjcYrl8XH2UTLsSks03IyAoEc041br7da7MoHpLDIj3yDTQIm2J0rNKttnpYJK/wtaE5wz
UGIiJPqtKdJK2KieAT3M9fDvEXjJ+cCzrRGrBT4CRk0S11qhUIemqiW7acb+35VeLR0ayLSMwX89
P2s64daNFE8lME52s2KCusDLGRFPi2CdSyeuZoHb4kZWeFgpEE+8mYmxacU9p3xRmHvB+OU0IU2c
XrQsV3XDK7O0zyuuTVkHK3W0YyjfodrgGiCU1eGFuVIyVr6ywAbxKvov/b7lq0zZss5hL9qo+eq3
ad/5Tdq3mK6UtIaSlYo1XXVn+azp2Sth11urxmzFBLVjNl/9Mdt3btUqekdvHE4sEY6jfri1RWuW
meUxq9eZP5cXWl5eb9SezGftWPQarx2IfPNHoW+uNwQjO1q7bKFcyRQWtKFYo9Z7qYQ+KpjCt6zb
naKdyDpWVv0in1jnq2xRBIo+q/yUHsC0enb6KrCzvOaTKGeBNAK6KkC0jf2e3NlmXzRnB3+NmmN2
9HBRIpPJJFUXCdabtlZ5UbDenyN3gEmdEFPGJ+1Fnr9fzkqOFgZ3G+M5eMVNQS/k7ERyzASDu83V
n0TX0Jex4pryYejKe6pGvFqxhzYOCSUbfVXIywUkbg7G9WlNa8exc6k4Ylfls5x+fvKrX8IGHcYy
DreLeLc8PTWarMzuShnwTXny0r2ymi/32/x0aV9L82+9pt95NFXR0JtbXRC9L6G9d0hNm/KVQZgP
AfGi764DF9pV2AyAN3hjSZuvVpE2DgA8qK8mVjqOPQtRDR6UFbzl5kCIL6+4nzvXpLimqbufgkFe
3n0SzbLR++Ac8eyDup7iTBlCLKldNGONWZEQyi5bMZHlVCNZEv/DdS5+6MeOJsn0vVXyEruhmFHM
MmL252JMpA7ffJs38QbOyNqMOsASKa5muPFokibz2NrhJ5AIijgwSo6p5hggpcjsatrj2nTH1STB
zciBq2/1a9zoN73NXadOR7Omy8rFrf2FF+ray3vdVW1xw1ua/juTVVmAcA0575Gq5lJe4VgmLtLX
utxXc+P7Js6BPdls2+RLpbuRY9mLHMm4WeLMqnROsW25AP07kL7/MhJVRmvqdOME4HYjoBEK19tj
IsyYmHX7dQR871NOr/nWdbuWYdF21zIsUDFX2ZWaGZU/18rPMMAVMlNz51Xm/44VtZrmT6FP+/4V
PI7V8gPxJs6OEN3+1/AfHihtNNDYBcZSpGe6r/LI+xrzoBQ4IQh+8IStHqLRhOOwwD5uemHISSV/
TZiIu0UltANUM0UUhokA7NA4JxdtjWGtahwJ5TBfTsuBHMT7mqM5RBz9DwEx2EtbIg9EJ7icoHOT
hiSwLIIwsId8SKWudQk6T48GosgxbkEDKJXZ986s64CXxZdz1Ln6MMkROO6HRIdEnhvA3qb/aPNB
GF18DLXvpF8lU2ITsQTLLFvn2YmATzlW2WEuCikhY6pESswrE4RnCGN0YElL9ImXRcOnSVetkIG/
ip9juCr+Qjzc3GqbycdvpolabjG5H3gIDY4kmpGh9NZtqtlJNVEh1G6+8YtfeiPt+G24C+5WB+xv
1wq31GB0fkCLdumTF9nCOpTBzNFcwEf5+KKJ/3iMr7M8qHIUKFoVSFZYMWPfhuKefyrydqkTgt4u
HuWPsteh+lUcFFg0Xmn7UuOE7vrzzYdCow9nKRKO0POEqwyy1lSl6jvsGvOsVqq9q0VOjXd2OBpj
nRO2YKxywtFbk5wrxg67z7JVZ4kUlJhXyAUTvgenfX9zcx2MiB1LmcGmSlWuWqFe+rBXWXKulAPR
Gmw1vIbmqzNOe55pdgRCaOOseK/0irhHOgMB+lPppUCq42g+BnXrALVGV4Div4aCXeI1DW6mZ8dY
XcXKYCGqd1PDLl9TPWsIpeT8H1aZ7JalzhzQF9hUFcjo29WvdlgxBa7tdIVQaIVpYjkX71rlypWz
Z9gwY6kFh7Wb7v3pm5a6k2qNga84ri7YgOMj1pktqVoxUIs4ph3UfD+qbEnF/qsqBzp2IU/ucoS8
u6KG4BCjoDn+m8RYEsiuRNWjIpIec5XdVVElwmWMq4h12e7g0h8AUMtkT0O3GKRF3y8fxk9w1+gd
hHklXCyGTHbWHO9vnrC57uI0ARM0uQABmU+DYEBeOxBlQX1+fkocgUbeM6H+JBCZs1ww1gUasmTq
NaOu6Hrbsp8Nwr8h4BlXLqxk3NpPTXLgbKHPvXw8d1abU1lZSeGMpT103LsO1JVsLdge0r+Gff03
tbhWL+4sS9cDVhBA8koos0aLJVjzDp7H8q0JRsmmdTZMBkJvSoCMwov/4JxAf3UYpougv5oWEBGC
uVv41iib9lzNbzy09PpVqADt3dx21JmNhiO7E/1ghajWsndDpXO6G8uCuLmJsWq0ozPBsPyzZNwY
OCKpgo0ESbR+DODiU41h6vkTfIRFZuY3ptb6b3b98MwFVZ4ipvOMMAvtngZOgaBMDVPdAavgnuDU
KcXKRnphiRuZinMNYxPbM0aqwVfZWPpYw0cfnnrR0mS5r3RXtNafNew0O2xYr0Hr6tFdyUJfBmi6
dnW8FTE76s0YeMhHJHRYr7yi6lWmNXjD4xqvc1+F7Lfh4VZwo46iWUsPGaZDlBlBaBVPw0nwRy9v
cuI37YkXldSo5bOelp4QWTQ+BIjM47k/B6UFC3UNBXYoJdfp++rbm8//n1fM/9dcW2/VAgsxa/m4
koL0kHzVXyZ/r+egwtqG3jN2aefpMX08NlZnjObjqptNoEarlzlUFGp+K/kitQ6YaVFOxRtXiQA6
3CkS0K+gA4IzLGWvcYZLd/8NbuufCeTZVXaUZeH/jWRsgOBrCkslxrNnqu/rLDkPlVhpRk9P85xj
Rjv9bA2OdzUi5tU0mUF0kC6goNX8Mi1OaWa0CXRJrr4DAjHqNTRe11m3zyRveJVXnFJ7Sdnz9gsl
K7h9fvGH+KWNX7sPZHn45tGjlflf8DvI/7D5+NGD+/9P9OjXHhie/8vzP2D/93e2n73c6Z6Nf6U+
rsj/82Brq5T/ZwsAc5v/40s80DXP8o7JK10btLLRODzPOUtHkIjT5BNm43pYTS/YDKHxU/Qcrqg/
RU8lBVnBP8/OMiow/mP0U+OnTqdj/5+KDyV4FIevFLl8RyNwGr7OeNsnc7Fq1FCbrG/IJUvoFN38
GZGPMg02j/x/EbcvrSFE5Mh0YkLhyyygfuR8YByBiYrNL2YmGn10sPf6IPo6gq5Ze3AFfmo0qq3b
6NNsiqWhNi/yJYQiUDxnC1ql4XBIxOBpo7tBY+/wLLrFqYYeRZxQWlM8d2zcbDj5YxAs2ufcE1G3
yGcFp9NY1Q6cObQdRIMoot/sPNs93Ns38rpxaqaC/cWo3Ix4EEMiiDKTXYV+fsBIwIhhQqywmSUF
JoXAprIvdws/WmkD+aqJNRhiHQfb3+0gsPTQaOLH6WySX6g7ZDZi704zKHYnif5kVc7JomFD2IoC
WqoUCyQ8R0J1yVtLM0YWPHZ3LiS3BY0N8cpTdRDOpo26oSK1dluhyni+6FgAfxBeqOtot9G4Awlp
OoKYa8QqKwyg0djqRvfuHboAr/6RunePx7e6a2KiaFnnBOnpZMICkm1NNcIurezgQ8dVowVBIjPP
xpzqQ8Q0vJjR+2l+1G3cx0C2p85Cc/gbzcD9/d7LnQ0GVR0QgIlOqrEX9nPp4pz8QOcEY8FGslhD
c4Kw3vy7HKe9i7VgQeuYo7tLONuCjrBN8AH5kJVDzTGBBXsM2dCDjB8gZ+w2HmDslYOloyVAhYRs
GuyowMBZVkACyUlJFtFwf+fVs539weHe3ouDwcHO0/2dw4PB8739pzv9rSGbPJwns+g+7/gDjsQ7
OkVjHNoe8H2eEPwmxwu2DEHMD8SjN6ZXJ9lCwOAVD0KmIKkzcEy9s9KOhv++oeFjN3BkN6jABtBP
d/FxMeR9h5WMO4yjfHYR5cdV5NKNhl3qODuZ0hYMjeSbFZdHH7J8KdmUwIkWJulJ2iBQXEhiToFp
SfI+uTBI23ipM37CHB3+pIFkHLaYw1ahMQbATLCHsDXsBtO9JfH/ER/Qf+76/XX6uIL+2yKqr0z/
PXrw6Jb++xLPnZXkVtujt3DWleYyly5wpk9xdBt3GncQn6EQl1AbHFyJKgkWxrQK8NyE/VZFp9el
MYgdLWsXs4IaKohWyZBEFdH+LuSrSflkPE3YazpAj8DHJpsYBBup5pbMJQeX0DCw7dLJaJagxTw5
Ps5GdCN0G94d2d/IZ4sNBBNtvN7bP+yDLdlED8+c5SNhxY7e9Lbus+2D77/d295/1t+qvKKGDw77
m13+v+pXr5/Kt8M3u33uHqG94cMenSxpDHNOMMWmH4j4Fz2fp2lk7B1oUK/2nu0M9l4f7u69Ouh3
OkjHm0/Gksyb88/2t+7/rvFy+8WLvaeDbboytwcvt/+lf7+x9/L14NWbl4PD78EgHtBk9l7vvPr2
xfZB6fXLH16U3hzu/bDzave/7+wfDF5v71PTOy92D172OXqLmRhRgq8OB0+3n36/gw4HB1S+v/W4
7vPusxc7g8PDF7jD915RD/8s+yC0h+406LzlLFKVWNGNdqdw0xLq+7u957vUBjJO8NiYZEjp/tYP
z/degFKAM08/jG7HjZtiB4fbhzuD1/s7z3f/xZTjAh1b4s+vng52Xx3u7P+47cb7gMarBTDV7f2n
3+/+SL+/xV7/p16cjoP49fq4Cv8/eFDC/5vfPP7mNv/nF3nuCHs7X05sfk/2mpvlgtCHIBAqHC4M
L4jNXSCPAGfvFNsOAvk3OweM/h2LDLzLn6k14vwOcFwvAiZdOQbI0hFR77xQStxlzRidJtMTlx2O
WgIDkUyYHp+CUuYkb/YOYgKcEH9GxKtN2MeDfv3m2xe7T0XXkfFtUiTHyMSn9HuXqxsXcGrNZ3mZ
3RXe68jjWbrRd4ZF5wsmycQlrMfDwbOGyecyMl8vn6TfK41tDfcgWfcMSwQC/Y43c1yoNKUcSZ/q
bnHlONrKxPMN6/XduLOar4+Ur/fZ+m5DWIh8OmCIguqhEyGl8mCenqQfoQez4PSvgCf858NvGiKh
2JcoDExyDNes2VBsB2gZetH+zusX2093Bn/aPfyeh7G/83T39S5dHrcMye1z+9w+t8/tc/vcPrfP
7XP73D63z+1z+9w+t8/tc/vcPrfP7XP73D63z+1z+9w+t8/tc/vcPrfP7XP73D63z+1z+9w+t8/t
81/u+f8BlgmkowBYAgA=
