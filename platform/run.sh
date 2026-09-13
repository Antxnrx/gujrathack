#!/usr/bin/env bash
#
# Bring the whole platform up: the registry (Model 1) and the control room
# (Model 2), with their databases, object store and both front ends.
#
# The two models are separate deployments on purpose — the registry is an
# infrastructure service that other models depend on, and folding it into one
# compose file would imply a coupling that does not exist. What this script does
# is start them in the order their dependencies require and then tell you the
# four URLs that matter.
#
#   ./platform/run.sh          start everything
#   ./platform/run.sh stop     stop the services this started
#   ./platform/run.sh status   what is up, and what each port is
#
# Ports are assigned so the two stacks never collide:
#
#   8000  registry API          5432  registry Postgres + PostGIS
#   3000  registry portal       5434  control-room Postgres + pgvector
#   8001  control-room API      9000  MinIO (detection crops)
#   5173  control-room console  6379  Redis (shared)
#
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
M2="$(cd "$HERE/.." && pwd)"
# The registry lives beside the control room by default; override if not.
M1="${MODEL1_DIR:-$(cd "$M2/../Guj-Govt-Hackathon-Model-1" 2>/dev/null && pwd)}"

say()  { printf '  %s\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$*"; }
head_() { printf '\n\033[1m%s\033[0m\n' "$*"; }

port_up() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }

# Start a long-running service and let go of it.
#
# The redirects are the point: a child that inherits this script's stdout keeps
# the pipe open, so whatever invoked run.sh waits for the servers rather than for
# run.sh. exec replaces the subshell instead of leaving a shell parked around the
# process, and disown drops it from the job table so no SIGHUP reaches it when
# the script exits.
start_bg() {
  local dir=$1 log=$2; shift 2
  ( cd "$dir" && exec "$@" </dev/null >"$log" 2>&1 ) &
  disown 2>/dev/null || true
}

wait_for() {  # wait_for <url> <label> <tries>
  local url=$1 label=$2 tries=${3:-60}
  for _ in $(seq 1 "$tries"); do
    if curl -fsS -o /dev/null -m 3 "$url" 2>/dev/null; then ok "$label"; return 0; fi
    sleep 1
  done
  bad "$label — did not answer at $url"
  return 1
}

status() {
  head_ "Services"
  for spec in \
    "8000|registry API" "3000|registry portal" \
    "8001|control-room API" "5173|control-room console" \
    "5432|registry Postgres" "5434|control-room Postgres" \
    "9000|MinIO" "6379|Redis"; do
    port="${spec%%|*}"; label="${spec#*|}"
    if port_up "$port"; then ok "$(printf '%-5s %s' ":$port" "$label")"
    else bad "$(printf '%-5s %s' ":$port" "$label")"; fi
  done
}

stop_all() {
  head_ "Stopping"
  pkill -f "ml.analytics.service" >/dev/null 2>&1 && say "stopped the analytics fold"
  for port in 5173 8001 3000 8000; do
    pid=$(lsof -ti tcp:"$port" 2>/dev/null || true)
    [ -n "$pid" ] && kill $pid 2>/dev/null && say "stopped :$port"
  done
  ( cd "$M2" && docker compose stop setu-pg setu-minio >/dev/null 2>&1 ) && say "stopped control-room containers"
  [ -d "$M1" ] && ( cd "$M1" && docker compose stop db >/dev/null 2>&1 ) && say "stopped registry container"
  echo
}

case "${1:-start}" in
  stop)   stop_all; exit 0 ;;
  status) status; echo; exit 0 ;;
esac

if [ -z "$M1" ] || [ ! -d "$M1" ]; then
  bad "Cannot find the registry. Set MODEL1_DIR to its directory."
  exit 1
fi

head_ "Databases and object store"
( cd "$M1" && docker compose up -d db >/dev/null 2>&1 ) && ok "registry Postgres (PostGIS) :5432"
( cd "$M2" && docker compose up -d setu-pg setu-minio >/dev/null 2>&1 ) \
  && ok "control-room Postgres (pgvector) :5434" && ok "MinIO :9000"

head_ "Services"
# The registry starts first: the control room verifies operator tokens against
# its JWKS, and the console will not render a signed-in shell without it.
if ! port_up 8000; then
  start_bg "$M1" /tmp/setu-m1-api.log .venv/bin/python -m uvicorn app.main:app --port 8000; fi
wait_for http://localhost:8000/healthz "registry API :8000"

if ! port_up 8001; then
  start_bg "$M2" /tmp/setu-m2-api.log .venv/bin/python -m uvicorn backend.app.main:app --port 8001; fi
wait_for http://localhost:8001/api/health "control-room API :8001"

# npm run dev is avoided deliberately: a colon anywhere in the project path
# breaks the PATH npm builds for lifecycle scripts, and "next: command not
# found" is a confusing way to learn that. Invoking the binaries directly is
# immune to it.
if ! port_up 3000; then
  start_bg "$M1/web" /tmp/setu-m1-web.log node node_modules/next/dist/bin/next dev; fi
wait_for http://localhost:3000/ "registry portal :3000" 90

if ! port_up 5173; then
  start_bg "$M2/frontend" /tmp/setu-m2-web.log node node_modules/vite/bin/vite.js --port 5173; fi
wait_for http://localhost:5173/ "control-room console :5173" 90

# The fold. Detections are written by the inference worker; this is what turns
# them into tracks and events, and without it a live feed fills the database and
# the timeline stays empty. It has no port to wait on, so it is checked by
# process name.
if ! pgrep -f "ml.analytics.service" >/dev/null 2>&1; then
  start_bg "$M2" /tmp/setu-analytics.log .venv/bin/python -m ml.analytics.service
  sleep 1
fi
if pgrep -f "ml.analytics.service" >/dev/null 2>&1; then
  ok "analytics fold (every 60s)"
else
  bad "analytics fold did not start — see /tmp/setu-analytics.log"
fi

head_ "Open"
say "Control room   http://localhost:5173"
say "Registry       http://localhost:3000"
say "Registry API   http://localhost:8000/docs"
say "Control API    http://localhost:8001/docs"
echo
say "Sign in with any registry account, password Sentinel@2026:"
say "  root@gujarat.gov.in · analyst@gujarat.gov.in · viewer@gujarat.gov.in"
echo
say "Logs: /tmp/setu-m1-api.log /tmp/setu-m2-api.log"
say "      /tmp/setu-m1-web.log /tmp/setu-m2-web.log"
echo
