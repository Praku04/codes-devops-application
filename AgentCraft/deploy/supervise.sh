#!/usr/bin/env bash
# Keep one service alive in a tmux pane, and when it dies, say why on the screen.
#
#   bash deploy/supervise.sh <label> <port> <command string>
#   bash deploy/supervise.sh ui 4225 'npm start'
#
# ── Why this exists ─────────────────────────────────────────────────────────────
#
# The UI pane used to end like this, minutes after a clean start, with the backend
# pane still happily serving health checks beside it:
#
#     Watch mode enabled. Watching for file changes...
#     Terminated
#     ubuntu@ip-10-92-27-51:~/projects/AgentCraft-HexaAgent/frontend$
#
# `Terminated` is bash reporting that its foreground child died of **SIGTERM** — signal
# 15, sent from outside. So the dev server did not crash, did not hit a build error and
# did not exit on its own: something on the box asked it to stop. Nothing in this
# repository sends that signal (`npm start` -> `frontend/scripts/serve.cjs` only ever
# mirrors a signal its child already received), and the pane then ran `exec bash`, which
# replaced the dead job with a prompt — so the single line naming the cause scrolled into
# a shell prompt and the stack was left half up with no explanation.
#
# Two separate problems, and this script fixes both:
#
#   1. **Nothing brought it back.** One SIGTERM to one process left the UI down until
#      somebody noticed and re-ran the command by hand. It now restarts, with a backoff
#      and a cap so a genuine build error does not turn into a spin loop.
#
#   2. **Nothing said why.** `Terminated` on its own is unattributable. On every exit
#      this prints the signal by name, how long the process had been up, the machine's
#      memory at that moment, and any out-of-memory record the kernel or an OOM daemon
#      logged in the same window — which is what turns "it keeps dying" into a cause.
#
# The overwhelmingly likely cause on a small instance is memory. Angular's dev server is
# the largest process on this box by a wide margin (it holds the whole bundle plus a file
# watcher in one Node heap), so it is the first thing any memory reaper picks — and the
# distinction matters, because `earlyoom` and most agent-style memory policing send
# **SIGTERM**, which is exactly what was seen, while the kernel's own OOM killer sends
# SIGKILL and would have printed `Killed` instead. `run-tmux.sh` now warns up front when
# the box has little RAM and no swap, which is the condition that invites it.
#
# ── Deliberately not here ───────────────────────────────────────────────────────
#
# This never kills anything by name or pattern. If the port is still held after the
# child exits, it reports the holder and waits instead of guessing which `node` on a
# shared box was ours — a `pkill -f node` here would take out someone else's project.
set -uo pipefail

LABEL="${1:?usage: supervise.sh <label> <port> <command>}"
PORT="${2:?usage: supervise.sh <label> <port> <command>}"
CMD="${3:?usage: supervise.sh <label> <port> <command>}"

# A run shorter than this is treated as a failure to start rather than a crash after
# service, which is what separates "something killed it" from "it cannot start at all".
MIN_UPTIME=25
MAX_FAST_FAILURES=4
BACKOFF=3
BACKOFF_MAX=30

say() { printf '[supervise %s] %s\n' "$LABEL" "$*"; }

# `kill -l 15` -> TERM. Anything at or above 128 from a shell is a signal death.
signal_name() {
  local n="$1" name
  name="$(kill -l "$n" 2>/dev/null || true)"
  printf '%s' "${name:-signal $n}"
}

memory_now() {
  [ -r /proc/meminfo ] || return 0
  # MemAvailable is the number that matters (free plus what is reclaimable), but it only
  # exists on Linux 3.14 and later and is absent under emulated /proc, where reporting a
  # flat 0 MB would read as "the box is out of memory" — the very thing being diagnosed.
  # So fall back to MemFree and say which one is being shown.
  awk '/^MemTotal:|^MemFree:|^MemAvailable:|^SwapTotal:|^SwapFree:/ {
         gsub(":", "", $1); v[$1] = int($2 / 1024)
       }
       END {
         if ("MemAvailable" in v) printf "  memory : %d MB total, %d MB available", v["MemTotal"], v["MemAvailable"]
         else                     printf "  memory : %d MB total, %d MB free", v["MemTotal"], v["MemFree"]
         if (v["SwapTotal"] > 0) printf " · swap %d of %d MB free", v["SwapFree"], v["SwapTotal"]
         else printf " · no swap"
         printf "\n"
       }' /proc/meminfo
}

# Anything that logged a memory kill while our child was dying. journalctl needs the
# `adm` or `systemd-journal` group, and Ubuntu restricts `dmesg` to root by default, so
# both are allowed to fail — an empty answer says "no evidence available", not "not OOM".
oom_evidence() {
  local since="$1" hits=""
  if command -v journalctl >/dev/null 2>&1; then
    hits="$(journalctl --since "$since" --no-pager -q 2>/dev/null \
            | grep -aiE 'out of memory|oom-kill|oom_reaper|earlyoom|systemd-oomd|killed process' \
            | tail -n 4 || true)"
  fi
  if [ -z "$hits" ] && command -v dmesg >/dev/null 2>&1; then
    hits="$(dmesg 2>/dev/null \
            | grep -aiE 'out of memory|oom-kill|killed process' | tail -n 4 || true)"
  fi
  printf '%s' "$hits"
}

# `ss` ships in /usr/sbin, which is not on a non-root PATH on Debian and Ubuntu, so looking
# for it by name alone reports "no such tool" on the exact machines that have it.
SS=""
for candidate in ss /usr/sbin/ss /sbin/ss; do
  command -v "$candidate" >/dev/null 2>&1 && { SS="$candidate"; break; }
done

port_holder() {
  [ -n "$SS" ] || return 0
  # -p only names the process for sockets we own; without it the line still proves the
  # port is taken, which is the part that decides whether restarting can possibly work.
  # The port has to be preceded by `:` or `.` so that a queue length or a longer port
  # number that merely contains these digits is not mistaken for the socket.
  "$SS" -ltnp 2>/dev/null | grep -E "[:.]${PORT}[[:space:]]" || true
}

stopping=0
caught=""
child=""

# A deliberate stop must not be restarted, or the pane becomes impossible to quit — but
# *which signal arrived* decides that, and the first version of this script got it wrong.
#
# It treated any of INT, TERM and HUP as a request to stop, and the pane duly reported
# `stopped on request after 572s — not restarting.` when nothing had asked for anything.
# The reason is that the killer signals the **whole process group**, not just the biggest
# process in it: this script sits in the same group as the service it runs, so it is hit
# alongside it. (That is also why the backend pane survives untouched — it is a different
# process group.) So a signal reaching *this* script is not evidence of intent at all.
#
# What separates intent from a reaper is the signal itself:
#
#   SIGINT   Ctrl-c in the pane.                                   deliberate
#   SIGHUP   the pane was destroyed — `./run-tmux.sh stop`,        deliberate
#            `tmux kill-session`; the pty closes and HUP follows.
#   SIGTERM  what earlyoom, systemd-oomd and every other memory    NOT deliberate
#            reaper sends. A person stopping a pane types Ctrl-c.
#
# So TERM now means restart, and only INT and HUP stop for good. The trade is that
# `kill <pid>` — which sends TERM by default — no longer stops a pane; the message says so
# and names Ctrl-c instead. Worth it: an unattributable stop is the failure being fixed.
#
# The signal is recorded rather than merely counted, because "stopped on request" without
# naming the signal was exactly the missing datum that made the pane above unreadable.
#
# The child runs as a tracked background job so `wait` can be interrupted out of it — a trap
# does not run while a foreground command is in progress — and the trap passes the signal on
# by the PID recorded above, never by name or pattern, so nothing else on a shared box can be
# caught by it.
on_signal() {
  caught="$1"
  case "$1" in
    INT | HUP) stopping=1 ;;
  esac
  [ -n "$child" ] && kill -"$1" "$child" 2>/dev/null
  true
}
trap 'on_signal INT' INT
trap 'on_signal TERM' TERM
trap 'on_signal HUP' HUP

fast_failures=0
signal_kills=0
attempt=0

while :; do
  attempt=$((attempt + 1))
  [ "$attempt" -gt 1 ] && say "start attempt $attempt: $CMD"

  caught=""
  started="$(date +%s)"
  # Backgrounded so the trap above can fire while it runs. Job control is off in a script, so
  # the child stays in this script's process group — which is the pane's foreground group — and
  # Ctrl-c still reaches it directly, exactly as it did when this ran in the foreground.
  bash -c "$CMD" &
  child=$!
  # `wait` returns early when a trap fires, and its status is then the signal, not the child's.
  # So keep waiting until the child is genuinely gone; only the last status describes its exit.
  while :; do
    wait "$child"
    status=$?
    kill -0 "$child" 2>/dev/null || break
  done
  child=""
  ended="$(date +%s)"
  uptime=$((ended - started))

  if [ "$stopping" -eq 1 ]; then
    echo
    say "stopped on request — SIG$caught after ${uptime}s up. Not restarting."
    case "$caught" in
      HUP) say "  (SIGHUP: the pane was closed — './run-tmux.sh stop' or 'tmux kill-session'.)" ;;
      INT) say "  (SIGINT: Ctrl-c.)" ;;
    esac
    break
  fi

  if [ "$status" -gt 128 ]; then
    signum=$((status - 128))
    reason="killed by SIG$(signal_name "$signum") (signal $signum)"
  elif [ "$status" -eq 0 ]; then
    reason="exited cleanly (status 0)"
  else
    reason="exited with status $status"
  fi

  echo
  say "$(date '+%Y-%m-%d %H:%M:%S') — $reason after ${uptime}s up."
  memory_now

  # SIGINT and SIGHUP are how a person and `run-tmux.sh stop` respectively ask for a stop.
  if [ "$status" -eq 130 ] || [ "$status" -eq 129 ]; then
    say "that signal means a deliberate stop — not restarting."
    break
  fi

  if [ "$status" -eq 143 ] || [ "$status" -eq 137 ]; then
    signal_kills=$((signal_kills + 1))

    # This script being signalled too means the *process group* was targeted, not one process.
    # Worth stating outright: it rules out the app killing itself, and it is what earlyoom-style
    # group kills look like from the inside.
    if [ -n "$caught" ]; then
      say "this supervisor was signalled as well (SIG$caught), so the whole process group was"
      say "  targeted — not just the service. Nothing inside the app can do that."
    fi

    evidence="$(oom_evidence "$((uptime + 120)) seconds ago")"
    if [ -n "$evidence" ]; then
      say "the system logged a memory kill in the same window:"
      printf '%s\n' "$evidence" | sed 's/^/         /'
      say "so this was almost certainly memory pressure, not a fault in the app."
    else
      say "no memory-kill record was readable (that is not proof it was not one:"
      say "  journalctl needs the adm group, and dmesg is root-only on Ubuntu)."
      say "  to check by hand: sudo journalctl --since '-10 min' | grep -i -e oom -e killed"
    fi

    # Restarting is still right — the service is wanted up — but after a few of these the
    # honest answer is that the box cannot host a dev server, and repeating an eight-minute
    # build that keeps being reaped is not a fix. So the advice escalates rather than repeats.
    if [ "$signal_kills" -ge 3 ]; then
      say "that is $signal_kills memory kills now. This machine cannot keep a *dev* server up:"
      say "  the bundler and file watcher live in one Node heap, which makes it the largest"
      say "  process here and so the first one reaped, every time. Build once and serve the"
      say "  output instead — no watcher, a fraction of the memory, and nothing to reap:"
      say "    cd frontend && npm run build      # then serve frontend/dist/ from nginx"
      say "  Restarting anyway, because the service is wanted up."
    else
      say "if this keeps happening, the durable fix is to stop running a *dev* server here:"
      say "  cd frontend && npm run build   then serve frontend/dist/ from nginx,"
      say "  or add swap so a spike does not make this the biggest thing to reap:"
      say "  sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile \\"
      say "    && sudo mkswap /swapfile && sudo swapon /swapfile"
    fi
  fi

  held="$(port_holder)"
  if [ -n "$held" ]; then
    # Restarting straight into `Address already in use` would replace a clear cause with
    # a misleading one, so wait for the socket instead of racing it.
    say "port $PORT is still held — waiting for it to clear rather than restarting into"
    say "  'Address already in use'. Holder, as far as this account can see it:"
    printf '%s\n' "$held" | sed 's/^/         /'
    say "  if it never clears:  sudo ss -ltnp | grep :$PORT   then stop that pid by number."
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      sleep 3
      [ -z "$(port_holder)" ] && break
    done
  fi

  if [ "$uptime" -lt "$MIN_UPTIME" ]; then
    fast_failures=$((fast_failures + 1))
    if [ "$fast_failures" -ge "$MAX_FAST_FAILURES" ]; then
      echo
      say "gave up: $fast_failures starts in a row lasted under ${MIN_UPTIME}s."
      # A short run is usually a build or config error, but not always: the UI's memory peak
      # is *during* the build, so a reaper can take it before it ever finishes serving. Saying
      # "failure to start" there would contradict the SIGTERM diagnosis printed a few lines
      # above and send whoever reads it looking for a bug that is not in the app.
      if [ "$status" -eq 143 ] || [ "$status" -eq 137 ]; then
        say "  Each of those was a signal, not an error — it is being stopped mid-startup,"
        say "  which is what memory pressure looks like when the peak is the build itself."
        say "  Restarting cannot win that race, so it stops here; fix the memory first."
      else
        say "  That is a failure to start rather than something killing a healthy process."
        say "  The real error is in the output above — restarting would only bury it further."
      fi
      # The backend command contains single quotes of its own, so wrapping it in more of
      # them would print a line that cannot be pasted back. %q quotes whatever is in it.
      say "  then:  bash deploy/supervise.sh $LABEL $PORT $(printf '%q' "$CMD")"
      break
    fi
    say "start $fast_failures of $MAX_FAST_FAILURES failed inside ${MIN_UPTIME}s."
  else
    # It served for a while, so whatever happened was not a startup problem: treat the
    # next failure as the first one again rather than carrying a stale count forever.
    fast_failures=0
    BACKOFF=3
  fi

  say "restarting in ${BACKOFF}s.  (Ctrl-c to stop for good.)"
  sleep "$BACKOFF"
  BACKOFF=$((BACKOFF * 2))
  [ "$BACKOFF" -gt "$BACKOFF_MAX" ] && BACKOFF="$BACKOFF_MAX"
  echo
done
