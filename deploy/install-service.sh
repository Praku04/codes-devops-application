#!/usr/bin/env bash
# Install AgentCraft as systemd services, so the backend and UI survive logging out,
# crashing, and rebooting.
#
#   sudo ./deploy/install-service.sh          install + start both
#   sudo ./deploy/install-service.sh api      backend only
#
# Why this exists: run-tmux.sh (and any bare `./run_api.sh`) is tied to the shell that
# started it. Close the SSH session, let the process crash, or reboot the instance, and the
# backend is simply gone — which shows up as the CLI and the UI both failing to connect for
# no visible reason. systemd owns the process instead, restarts it when it dies, and starts
# it at boot.
#
# tmux is still the better choice while actively developing: logs in front of you, Ctrl-c to
# restart. This is for a box you want to stay up.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WHICH="${1:-both}"

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo: sudo $0 $WHICH" >&2; exit 1; }

# The user who owns the checkout, not root: the app writes agentcraft.db, workspaces and
# exports next to the code, and root-owned files there would break the next non-root run.
RUN_USER="$(stat -c '%U' "$ROOT")"
[ "$RUN_USER" = "root" ] && { echo "Refusing to run the app as root — checkout is root-owned." >&2; exit 1; }

read_key() {
  local key="$1" found="" f v
  for f in "$ROOT/.env.example" "$ROOT/.env"; do
    [ -f "$f" ] || continue
    v="$(sed -n "s/^[[:space:]]*${key}[[:space:]]*=[[:space:]]*//p" "$f" | tail -n 1 | tr -d '"'"'"'' | tr -d '\r')"
    [ -n "$v" ] && found="$v"
  done
  printf '%s' "$found"
}

API_PORT="$(read_key API_PORT)"; API_PORT="${API_PORT:-8555}"
FRONTEND_PORT="$(read_key FRONTEND_PORT)"; FRONTEND_PORT="${FRONTEND_PORT:-4225}"

install_api() {
  local venv="$ROOT/backend/.venv"
  [ -x "$venv/bin/python" ] || venv="$ROOT/.venv"
  [ -x "$venv/bin/python" ] || { echo "No .venv found in backend/ or repo root — create one first." >&2; exit 1; }

  # No --reload: it spawns a worker child, which confuses systemd's process tracking and is
  # pointless for a service that is not being edited in place.
  cat > /etc/systemd/system/agentcraft-api.service <<UNIT
[Unit]
Description=AgentCraft API
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
User=$RUN_USER
WorkingDirectory=$ROOT/backend
Environment=PYTHONPATH=$ROOT/backend
Environment=PYTHONUNBUFFERED=1
ExecStart=$venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port $API_PORT
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
  echo "wrote /etc/systemd/system/agentcraft-api.service  (port $API_PORT)"
}

install_ui() {
  local npm_bin
  npm_bin="$(command -v npm)" || { echo "npm not on PATH — install Node first." >&2; exit 1; }

  # `npm start` is Angular's dev server. Acceptable for an internal box; for real traffic
  # build once and serve frontend/dist/ from nginx instead of running this.
  cat > /etc/systemd/system/agentcraft-ui.service <<UNIT
[Unit]
Description=AgentCraft UI (Angular dev server)
After=network-online.target agentcraft-api.service
Wants=network-online.target

[Service]
Type=exec
User=$RUN_USER
WorkingDirectory=$ROOT/frontend
Environment=PATH=$(dirname "$npm_bin"):/usr/local/bin:/usr/bin:/bin
ExecStart=$npm_bin start
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
  echo "wrote /etc/systemd/system/agentcraft-ui.service  (port $FRONTEND_PORT)"
}

UNITS=()
case "$WHICH" in
  api)  install_api; UNITS=(agentcraft-api) ;;
  ui)   install_ui;  UNITS=(agentcraft-ui) ;;
  both) install_api; install_ui; UNITS=(agentcraft-api agentcraft-ui) ;;
  *)    echo "Usage: sudo $0 [api|ui|both]" >&2; exit 1 ;;
esac

systemctl daemon-reload
for u in "${UNITS[@]}"; do
  systemctl enable "$u" >/dev/null
  systemctl restart "$u"
done

echo
sleep 2
for u in "${UNITS[@]}"; do
  systemctl is-active --quiet "$u" && echo "  $u: active" || echo "  $u: FAILED — journalctl -u $u -n 40"
done

cat <<EOS

Running as user '$RUN_USER', restarted automatically, and started on boot.

  status : sudo systemctl status agentcraft-api
  logs   : sudo journalctl -u agentcraft-api -f
  restart: sudo systemctl restart agentcraft-api
  stop   : sudo systemctl stop agentcraft-api      (stays stopped until started again)
  remove : sudo systemctl disable --now agentcraft-api agentcraft-ui

Check it: curl http://127.0.0.1:$API_PORT/health
EOS
