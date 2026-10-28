#!/usr/bin/env bash
# Start the whole stack in a detached tmux session — one window, two panes.
#
# Run from the repo root:  ./run-tmux.sh
#
# Works unchanged locally and on a server: the backend binds API_HOST (0.0.0.0 by default)
# and the UI resolves the API from whatever address the browser used, so there is nothing
# host-specific to set here.
#
#   ./run-tmux.sh          start (or re-attach to) the session
#   ./run-tmux.sh stop     kill it
#   tmux attach -t agentcraft
#   Ctrl-b o               switch pane      Ctrl-b d   detach, leaving both running
set -euo pipefail

SESSION="agentcraft"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

read_key() {
  local key="$1" found="" f v
  for f in "$ROOT/.env.example" "$ROOT/.env"; do
    [ -f "$f" ] || continue
    v="$(sed -n "s/^[[:space:]]*${key}[[:space:]]*=[[:space:]]*//p" "$f" | tail -n 1 | tr -d '"'"'"'' | tr -d '\r')"
    [ -n "$v" ] && found="$v"
  done
  printf '%s' "$found"
}

API_PORT="${API_PORT:-$(read_key API_PORT)}"
FRONTEND_PORT="${FRONTEND_PORT:-$(read_key FRONTEND_PORT)}"
API_PORT="${API_PORT:-8555}"
FRONTEND_PORT="${FRONTEND_PORT:-4225}"

# Every tmux target below is written `=agentcraft`, not `agentcraft`, and that leading `=` is
# load-bearing. tmux resolves a bare `-t name` loosely: exact match first, then any session whose
# name it is a *prefix* of, then fnmatch. So on a box where several things live in tmux,
# `kill-session -t agentcraft` with no exact `agentcraft` session happily kills `agentcraft-old`,
# `agentcraftui`, `agentcraft2` — someone else's stack, torn down by a script that then printed
# "Stopped agentcraft". `=` means this exact name and nothing else. Same for has-session and
# attach, where the loose match is how you end up attached to, and typing into, the wrong stack.
TARGET="=$SESSION"

if [ "${1:-}" = "stop" ]; then
  if tmux has-session -t "$TARGET" 2>/dev/null; then
    tmux kill-session -t "$TARGET" && echo "Stopped $SESSION"
  else
    echo "No '$SESSION' session — nothing stopped."
  fi
  # Everything else on this tmux server is deliberately left alone; listing it makes that
  # visible rather than something you have to go and check after the fact.
  others="$(tmux list-sessions -F '#S' 2>/dev/null | grep -vx "$SESSION" | paste -sd' ' - || true)"
  [ -n "$others" ] && echo "Untouched, still running: $others" || true
  exit 0
fi

command -v tmux >/dev/null || { echo "tmux not installed: sudo apt install -y tmux" >&2; exit 1; }

# Already running? Attach instead of starting a second copy on the same ports.
if tmux has-session -t "$TARGET" 2>/dev/null; then
  echo "Session '$SESSION' already running — attaching."
  exec tmux attach -t "$TARGET"
fi

# ── The virtualenv ───────────────────────────────────────────────────────────
#
# `source .venv/bin/activate 2>/dev/null` used to be the whole of this, and it was wrong
# twice over. It looked only in `backend/`, while a checkout that followed the README has the
# venv at the repo root — and `2>/dev/null` then threw away the one line that would have said
# so. The pane carried on to `run_api.sh`, which runs a bare `python`, so the API came up on
# the *system* interpreter and the mistake surfaced minutes later as a
# `ModuleNotFoundError: sqlalchemy` that looks nothing like its cause.
#
# So: search the places a venv actually lives, use an absolute path (the pane's own cwd is
# `backend/`, which is not where the venv has to be), and if there is none, say so loudly
# instead of pretending. `AGENTCRAFT_VENV` overrides the search.
find_venv() {
  local dir sub
  for dir in ${AGENTCRAFT_VENV:+"$AGENTCRAFT_VENV"} \
             "$ROOT/.venv" "$ROOT/backend/.venv" "$ROOT/venv" "$ROOT/backend/venv"; do
    # `bin` on Linux and macOS; `Scripts` for a venv built on Windows and driven from Git Bash.
    for sub in bin Scripts; do
      if [ -f "$dir/$sub/activate" ]; then
        printf '%s' "$dir/$sub/activate"
        return 0
      fi
    done
  done
  return 1
}
VENV_ACTIVATE="$(find_venv || true)"

if [ -n "$VENV_ACTIVATE" ]; then
  # `&&`, not `;`: if activation fails there is no point starting uvicorn, and stopping there
  # leaves the reason on screen. `\$VIRTUAL_ENV` is escaped so the *pane* expands it — which
  # environment is live is exactly what was invisible before.
  BACKEND_CMD="source '$VENV_ACTIVATE' && echo \"[venv] \$VIRTUAL_ENV\" && ./run_api.sh"
else
  BACKEND_CMD="echo '[venv] none found — using the system python; expect ModuleNotFoundError' >&2; ./run_api.sh"
fi

# ── Staying up ───────────────────────────────────────────────────────────────
#
# Both panes run under `deploy/supervise.sh`, because a clean start was not the same thing
# as a stack that was still up an hour later. The UI pane in particular ended as:
#
#     Watch mode enabled. Watching for file changes...
#     Terminated
#
# — SIGTERM from outside the process (nothing here sends it; on a small box a memory reaper
# picks the dev server first, since it is comfortably the largest process, and it signals the
# whole pane's process group rather than that one process). `; exec bash` then replaced the
# dead job with a prompt, so the one line naming the cause scrolled away and the UI stayed
# down until somebody noticed. The supervisor restarts it, and prints the signal by name, the
# uptime, the memory at that moment and any OOM record from the same window — so the next
# occurrence is diagnosable instead of just gone. Run via `bash …` rather than `./…` so it
# works from a checkout that never got its exec bit.
#
# `exec bash` stays as the last resort: if the supervisor itself gives up (a real build
# error, four failed starts in a row) the pane still keeps its shell and its output.
SUPERVISOR="$ROOT/deploy/supervise.sh"

# The supervised command contains both `'` and `"` (the venv path is single-quoted, the
# `[venv]` echo double-quoted), and it has to arrive at the script as one argument after
# the pane's shell has had its turn at it. `printf %q` is the only quoting that survives
# that round trip intact; hand-rolled quotes here silently split the command in two.
#
# If the supervisor is not there — someone copied this file out on its own — the pane runs
# the command bare, exactly as it did before. Losing the restart is a smaller problem than
# a stack that will not start at all, and the warning below says which one happened.
if [ -f "$SUPERVISOR" ]; then
  SUPERVISE="bash $(printf '%q' "$SUPERVISOR")"
  supervised() { printf '%s %s %s %s' "$SUPERVISE" "$1" "$2" "$(printf '%q' "$3")"; }
else
  echo "  supervise: WARNING — deploy/supervise.sh is missing, so neither pane will restart" >&2
  echo "             itself if it is stopped. Both still start normally." >&2
  supervised() { printf '%s' "$3"; }
fi

# Backend in the first pane, UI in the second.
tmux new-session -d -s "$SESSION" -n stack -c "$ROOT/backend"
tmux send-keys -t "$TARGET:stack.0" "$(supervised api "$API_PORT" "$BACKEND_CMD"); exec bash" C-m

# ── The UI's dependencies ────────────────────────────────────────────────────
#
# `npm start` on a checkout that was never installed fails with
# `Cannot find module '.../node_modules/@angular/cli/bin/ng.js'` — a MODULE_NOT_FOUND stack
# that names a path inside `node_modules` and never says "run npm install". A fresh clone or
# a `git clean` hits it every time, and the pane's own env preamble prints first, so it reads
# like the app broke rather than like nothing was installed.
#
# `@angular/cli/bin/ng.js` is the exact file `npm start` needs, so testing for it also catches
# the half-installed tree an interrupted install leaves behind, which a bare `-d node_modules`
# would miss.
# Node itself, before its packages: Angular 22's CLI refuses to run on anything older than
# v22.22.3, and a box shared with an older project is usually still on 18 or 20. Installing
# dependencies succeeds there, so the only symptom is `ng serve` exiting — which looks like an
# app failure rather than a runtime that is too old. `npm start` reports this properly now
# (frontend/scripts/check-node.cjs); this says it once more up front, where the ports are listed.
NODE_OK=1
if command -v node >/dev/null 2>&1; then
  NODE_VER="$(node --version 2>/dev/null || true)"
  NODE_MAJOR="$(printf '%s' "$NODE_VER" | sed -e 's/^v//' -e 's/\..*$//')"
  if [ -n "$NODE_MAJOR" ] && [ "$NODE_MAJOR" -lt 22 ] 2>/dev/null; then
    NODE_OK=0
  fi
else
  NODE_OK=0
  NODE_VER="not installed"
fi

# A third way for the tree to be wrong, and the least obvious: present, complete, and built for
# the wrong runtime. Vite's bundler is a compiled binary in an *optional* dependency, and npm
# skips optional deps whose `engines` do not match the Node running the install — so everything
# installed on Node 18 has no `@rolldown/binding-*` at all, and `ng serve` dies with a
# MODULE_NOT_FOUND for `../rolldown-binding.linux-x64-gnu.node` thirty frames deep inside vite.
# Upgrading Node does not repair it: npm sees a satisfied tree. Only a rebuild does, which is
# why this reinstalls rather than warning. (frontend/scripts/check-native.cjs says the same thing
# to anyone running `npm start` directly.)
ui_tree_ok() {
  [ -f "$ROOT/frontend/node_modules/@angular/cli/bin/ng.js" ] || return 1
  # Any platform binary at all: `npm ci` only ever fetches the one for this machine.
  ls "$ROOT"/frontend/node_modules/@rolldown/binding-*/*.node \
     "$ROOT"/frontend/node_modules/vite/node_modules/@rolldown/binding-*/*.node \
     >/dev/null 2>&1
}

# The install, when one is needed, is a *prefix* to the supervised command rather than part
# of it: a restart hours later must not reinstall node_modules, and a failed install must not
# be retried four times by the supervisor before the reason is readable.
UI_INSTALL=""
if ! ui_tree_ok; then
  echo "  ui     : node_modules missing or incomplete — the pane will install it first"
  # `&&`: a failed install must not roll on into `npm start` and bury the reason in a second,
  # unrelated error. `npm ci` when there is a lockfile — reproducible, and it deletes the tree
  # first rather than patching it, which is what a tree missing its platform binary needs:
  # `npm install` over one of those changes nothing, because npm counts a skipped *optional*
  # dependency as satisfied. Hence the explicit `rm -rf` in the no-lockfile branch.
  if [ -f "$ROOT/frontend/package-lock.json" ]; then
    UI_INSTALL='echo "[ui] installing dependencies (npm ci)…" && npm ci && '
  else
    UI_INSTALL='echo "[ui] installing dependencies (npm install)…" && rm -rf node_modules && npm install && '
  fi
fi

tmux split-window -t "$TARGET:stack" -h -c "$ROOT/frontend"
tmux send-keys -t "$TARGET:stack.1" \
  "${UI_INSTALL}$(supervised ui "$FRONTEND_PORT" 'npm start'); exec bash" C-m

tmux select-pane -t "$TARGET:stack.0"

echo "Started tmux session '$SESSION' (backend :$API_PORT · UI :$FRONTEND_PORT)"

if [ "$NODE_OK" -eq 0 ]; then
  echo "  node   : WARNING — Node $NODE_VER is too old for the Angular CLI (needs v22.22.3+)." >&2
  echo "           The UI pane will stop and print how to upgrade. The API is unaffected." >&2
  echo "           nvm install --lts && nvm alias default 'lts/*'   (per-user; safe here)" >&2
fi

# ── Memory ───────────────────────────────────────────────────────────────────
#
# Said here, before anything is running, because the failure it predicts arrives half an
# hour later looking like nothing at all: the UI pane prints `Terminated` and stops. That is
# SIGTERM from a memory reaper, and the dev server is what it picks — it holds the whole
# bundle plus a file watcher in one Node heap, so it is reliably the largest process on the
# box. A small instance with no swap has no slack at all: the first spike has to come out of
# something, and this is that something. The supervisor restarts it and names the cause, but
# an hour of restarts is worth less than the two commands below.
if [ -r /proc/meminfo ]; then
  MEM_TOTAL="$(awk '/^MemTotal:/ {print int($2 / 1024)}' /proc/meminfo)"
  SWAP_TOTAL="$(awk '/^SwapTotal:/ {print int($2 / 1024)}' /proc/meminfo)"
  # 2 GB: below this the Angular build alone is a substantial fraction of the machine.
  if [ -n "${MEM_TOTAL:-}" ] && [ "$MEM_TOTAL" -lt 2048 ] && [ "${SWAP_TOTAL:-0}" -eq 0 ]; then
    echo "  memory : WARNING — ${MEM_TOTAL} MB RAM and no swap. The UI dev server is the biggest" >&2
    echo "           process here, so it is the first thing a memory reaper stops — the pane" >&2
    echo "           says 'Terminated' and the UI goes down. Either add swap:" >&2
    echo "             sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile \\" >&2
    echo "               && sudo mkswap /swapfile && sudo swapon /swapfile" >&2
    echo "           or stop running a dev server at all: cd frontend && npm run build," >&2
    echo "           then serve frontend/dist/ from nginx. The API is not affected either way." >&2
  fi
fi

# A DATABASE_URL pointing at a host this machine cannot resolve kills the API at startup, and
# the reason scrolls past inside a SQLAlchemy traceback in the other pane. Checked here because
# resolution is one call and the answer is almost always "that URL belongs to another project".
# Only for a real server: sqlite is a file and has no host.
DB_URL="${DATABASE_URL:-$(read_key DATABASE_URL)}"
case "$DB_URL" in
  "" | sqlite*) ;;
  *)
    # Strip scheme, then any user:password@, then any :port or /database.
    DB_HOST="$(printf '%s' "$DB_URL" | sed -e 's#^[^:]*://##' -e 's#^.*@##' -e 's#[/?].*$##' -e 's#:.*$##')"
    if [ -n "$DB_HOST" ] && command -v getent >/dev/null 2>&1 \
       && ! getent hosts "$DB_HOST" >/dev/null 2>&1; then
      echo "  db     : WARNING — '$DB_HOST' does not resolve from this machine, so the API" >&2
      echo "           will fail to start. DATABASE_URL in .env points at it." >&2
      echo "           For the local file database: DATABASE_URL=sqlite:///./agentcraft.db" >&2
    fi
    ;;
esac
if [ -n "$VENV_ACTIVATE" ]; then
  echo "  venv   : ${VENV_ACTIVATE%/*/activate}"
else
  echo "  venv   : NONE FOUND — looked in .venv, backend/.venv, venv, backend/venv" >&2
  echo "           the API will use the system python and probably fail to import its deps." >&2
  echo "           python3 -m venv .venv && source .venv/bin/activate && pip install -r backend/requirements.txt" >&2
fi
echo
echo "  attach : tmux attach -t $SESSION"
echo "  detach : Ctrl-b d          switch pane: Ctrl-b o"
echo "  stop   : ./run-tmux.sh stop"
echo
echo "Open the UI on http://localhost:$FRONTEND_PORT/ — or, from another machine,"
echo "http://<this-host>:$FRONTEND_PORT/ with TCP $FRONTEND_PORT and $API_PORT open."
