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

REBUILD=0
NO_CACHE=0
PULL=0
DETACH=1
FREE_LIMITS=0
ENABLE_TUI=""
DRY_RUN=0
FOLLOW_LOGS=0

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
    up|build|down|stop|restart|logs|status|shell|cli|config|print-env|materialize|purge|update-embed|help)
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

CONTEXT_FILES=(Dockerfile scripts skills dashboard-plugins)

context_is_complete() {
  local dir="$1" f
  for f in "${CONTEXT_FILES[@]}"; do
    [ -e "$dir/$f" ] || return 1
  done
  [ -f "$dir/scripts/bootstrap.sh" ] || return 1
  [ -f "$dir/scripts/patch-config.py" ] || return 1
  [ -f "$dir/scripts/free-storage.py" ] || return 1
  [ -f "$dir/scripts/patch-model-discovery.py" ] || return 1
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
# Environment assembly (render.yaml parity)
# --------------------------------------------------------------------------

declare -A ENV_MAP=()
ENV_ORDER=()

set_env() {
  local key="$1" value="$2"
  if [ -z "${ENV_MAP[$key]+x}" ]; then ENV_ORDER+=("$key"); fi
  ENV_MAP["$key"]="$value"
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
  # --- Blueprint-managed boot config (render.yaml) -----------------------
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

  # --- User-supplied values ---------------------------------------------
  load_env_file "$ENV_FILE"

  local var
  for var in "${PASSTHROUGH_VARS[@]}"; do
    if [ -n "${!var:-}" ]; then set_env "$var" "${!var}"; fi
  done

  # --- Local overrides ---------------------------------------------------
  if [ -n "$ENABLE_TUI" ]; then set_env HERMES_DASHBOARD_TUI "$ENABLE_TUI"; fi

  # Keep the container's idea of its port aligned with the mapping.
  set_env PORT "$CONTAINER_PORT"
  set_env HERMES_DASHBOARD_PORT "$CONTAINER_PORT"
}

write_env_file() {
  local out="$1" key
  ( umask 077; : > "$out" )
  for key in "${ENV_ORDER[@]}"; do
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

case "$COMMAND" in
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
n/8fIfHxWgAIAgA=
