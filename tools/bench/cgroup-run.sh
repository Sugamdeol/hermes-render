#!/bin/sh
# Run a command under the same hard limits as a Render Free web service.
#
# Render Free gives the container 512 MB of RAM and 0.1 CPU. Measuring the
# gateway + dashboard on a workstation proves nothing about an OOM, so this
# wrapper puts the whole process tree inside a real cgroup with those limits
# before it execs the command.
#
# Two backends, chosen automatically:
#
#   docker   `docker run --memory --cpus` — closest to production, needs the
#            image to be built and a Docker daemon.
#   cgroup   a cgroup v2 slice created directly on the host. Same kernel
#            accounting and the same OOM killer, no image required, which is
#            what makes it usable for iterating on the Python code. Needs
#            passwordless sudo (it only ever writes under /sys/fs/cgroup).
#
# Usage:
#   tools/bench/cgroup-run.sh [--mem 512m] [--cpu 0.1] [--name hermes-bench] \
#       [--backend auto|cgroup|docker] [--image IMAGE] -- command [args...]
#
# Environment:
#   BENCH_CGROUP_ROOT  cgroup v2 mount point (default /sys/fs/cgroup)
#
# The command runs as the invoking user inside the limited cgroup; children
# inherit the limit, so the gateway, the dashboard and every git/python child
# they spawn are all accounted together -- exactly like the container.

set -eu

MEM="512m"
CPU="0.1"
NAME="hermes-bench"
BACKEND="auto"
IMAGE=""

usage() {
  sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
  exit 2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --mem) MEM="$2"; shift 2 ;;
    --cpu) CPU="$2"; shift 2 ;;
    --name) NAME="$2"; shift 2 ;;
    --backend) BACKEND="$2"; shift 2 ;;
    --image) IMAGE="$2"; shift 2 ;;
    -h|--help) usage ;;
    --) shift; break ;;
    *) echo "unknown argument: $1" >&2; usage ;;
  esac
done

[ $# -gt 0 ] || { echo "no command given" >&2; usage; }

# ── helpers ───────────────────────────────────────────────────────────

# 512m / 1g -> bytes
mem_to_bytes() {
  raw="$1"
  num=$(printf '%s' "${raw}" | sed -n 's/^\([0-9][0-9]*\).*/\1/p')
  unit=$(printf '%s' "${raw}" | sed -n 's/^[0-9][0-9]*\([a-zA-Z]*\)$/\1/p')
  case "${unit}" in
    ""|b|B) mult=1 ;;
    k|K|kb|KB) mult=1024 ;;
    m|M|mb|MB) mult=1048576 ;;
    g|G|gb|GB) mult=1073741824 ;;
    *) echo "cannot parse --mem ${raw}" >&2; exit 2 ;;
  esac
  echo $(( num * mult ))
}

MEM_BYTES=$(mem_to_bytes "${MEM}")

# 0.1 CPU -> "10000 100000" (quota period)
cpu_to_quota() {
  period=100000
  quota=$(awk -v c="$1" -v p="${period}" 'BEGIN { q = int(c * p); if (q < 1000) q = 1000; print q }')
  echo "${quota} ${period}"
}

CG_ROOT="${BENCH_CGROUP_ROOT:-/sys/fs/cgroup}"

have_docker() { command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; }
have_cgroup() {
  [ -f "${CG_ROOT}/cgroup.controllers" ] \
    && grep -qw memory "${CG_ROOT}/cgroup.controllers" \
    && sudo -n true 2>/dev/null
}

case "${BACKEND}" in
  auto)
    if have_docker; then BACKEND=docker; elif have_cgroup; then BACKEND=cgroup; else
      echo "no usable limiter: install Docker or allow passwordless sudo" >&2; exit 1
    fi ;;
  docker|cgroup) ;;
  *) echo "unknown backend ${BACKEND}" >&2; exit 2 ;;
esac

# ── docker ────────────────────────────────────────────────────────────

if [ "${BACKEND}" = "docker" ]; then
  [ -n "${IMAGE}" ] || { echo "--backend docker needs --image" >&2; exit 2; }
  exec docker run --rm -i \
    --memory="${MEM}" --memory-swap="${MEM}" \
    --cpus="${CPU}" \
    --pids-limit=256 \
    --name "${NAME}" \
    "${IMAGE}" "$@"
fi

# ── cgroup v2 slice ───────────────────────────────────────────────────

SLICE="${CG_ROOT}/${NAME}"
if [ -d "${SLICE}" ]; then
  # A previous run may have been killed; drop any stragglers first.
  sudo -n sh -c "for p in \$(cat '${SLICE}/cgroup.procs' 2>/dev/null); do kill -KILL \$p 2>/dev/null || true; done" || true
  sudo -n rmdir "${SLICE}" 2>/dev/null || true
fi
sudo -n mkdir -p "${SLICE}"
sudo -n sh -c "echo ${MEM_BYTES} > '${SLICE}/memory.max'"
sudo -n sh -c "echo ${MEM_BYTES} > '${SLICE}/memory.swap.max'" 2>/dev/null || true
sudo -n sh -c "echo '$(cpu_to_quota "${CPU}")' > '${SLICE}/cpu.max'" 2>/dev/null || true
# Report the limit to children that want to know (the bootstrap memory guard
# reads the cgroup itself, but tests may want the number directly).
export HERMES_MEM_LIMIT_MB=$(( MEM_BYTES / 1048576 ))
export BENCH_CGROUP_PATH="${SLICE}"

cleanup() {
  # Give the tree a moment to exit on its own, then force it, then rmdir.
  sudo -n sh -c "for p in \$(cat '${SLICE}/cgroup.procs' 2>/dev/null); do kill -TERM \$p 2>/dev/null || true; done" 2>/dev/null || true
  sleep 1
  sudo -n sh -c "for p in \$(cat '${SLICE}/cgroup.procs' 2>/dev/null); do kill -KILL \$p 2>/dev/null || true; done" 2>/dev/null || true
  sudo -n rmdir "${SLICE}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM


# The payload runs as the invoking user with its own environment intact --
# sudo would reset PATH/HERMES_* on the way through. Instead we start it
# blocked on a gate file, move its PID into the slice as root (root may move
# any PID), and only then release it. Everything the payload spawns inherits
# the cgroup, so the whole tree is accounted and limited together.
GATE="${TMPDIR:-/tmp}/.cgroup-run-gate-${NAME}-$$"
rm -f "${GATE}"

sh -c "while [ ! -e '${GATE}' ]; do sleep 0.05; done; exec \"\$@\"" sh "$@" &
PAYLOAD=$!

moved=0
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if sudo -n sh -c "echo ${PAYLOAD} > '${SLICE}/cgroup.procs'" 2>/dev/null; then
    moved=1
    break
  fi
  sleep 0.1
done
touch "${GATE}"
if [ "${moved}" -ne 1 ]; then
  echo "cgroup-run: could not move pid ${PAYLOAD} into ${SLICE}; limits NOT applied" >&2
fi

status=0
wait "${PAYLOAD}" || status=$?
rm -f "${GATE}"
exit "${status}"
