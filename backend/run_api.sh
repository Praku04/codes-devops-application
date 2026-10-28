#!/usr/bin/env bash
# Run from AgentCraft/backend — the Linux/macOS twin of run_api.ps1.
#
# Host and port come from the repo-root .env (API_HOST / API_PORT) so the backend, the
# Angular dev server and the CLI all agree — see .env.example. Uvicorn needs them on the
# command line, before the app is imported, hence the parsing here.
#
# This exists mainly for deployments: typing the uvicorn command by hand invites
# `--host 127.0.0.1`, which binds loopback only and refuses every connection from outside
# the box — the classic "it says it started but the browser can't reach it" on EC2.
set -euo pipefail

export PYTHONPATH="$(pwd)"

API_HOST_DEFAULT="0.0.0.0"
API_PORT_DEFAULT="8555"

# .env.example first so a fresh clone without a .env still starts on the documented port;
# a real .env then overrides it key by key. Real environment variables win over both.
read_key() {
  local key="$1" found=""
  for f in ../.env.example ../.env; do
    [ -f "$f" ] || continue
    local v
    v="$(sed -n "s/^[[:space:]]*${key}[[:space:]]*=[[:space:]]*//p" "$f" | tail -n 1 | tr -d '"'"'"'' | tr -d '\r')"
    [ -n "$v" ] && found="$v"
  done
  printf '%s' "$found"
}

API_HOST="${API_HOST:-$(read_key API_HOST)}"
API_PORT="${API_PORT:-$(read_key API_PORT)}"
PUBLIC_HOST="${PUBLIC_HOST:-$(read_key PUBLIC_HOST)}"
API_HOST="${API_HOST:-$API_HOST_DEFAULT}"
API_PORT="${API_PORT:-$API_PORT_DEFAULT}"

echo "Starting API on http://${API_HOST}:${API_PORT}"
if [ -n "$PUBLIC_HOST" ]; then
  echo "Reachable at http://${PUBLIC_HOST}:${API_PORT}  (PUBLIC_HOST)"
  if [ "$API_HOST" = "127.0.0.1" ] || [ "$API_HOST" = "localhost" ]; then
    # Worth shouting about: PUBLIC_HOST says this is a deployment, but the bind address
    # says loopback only, so nothing outside the machine will connect.
    echo "WARNING: API_HOST=${API_HOST} binds loopback only — set API_HOST=0.0.0.0 in .env" >&2
  fi
fi

exec python -m uvicorn app.main:app --reload --host "$API_HOST" --port "$API_PORT"
