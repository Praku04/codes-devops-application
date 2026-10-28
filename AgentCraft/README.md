# AgentCraft

Local studio that turns a problem statement (or interview) into **IDE-ready agents, skills, and rules** for **Claude Code**, **Cursor**, or **Windsurf**.

Same backend powers the **Angular UI** and the **CLI**.

## Architecture

| Piece | Tech |
| --- | --- |
| Backend | FastAPI + SQLite + LiteLLM (AWS Bedrock) |
| Frontend | Angular wizard + Sessions + Super Admin |
| CLI | Typer + Rich (`agentcraft`) |
| Auth | JWT; **new signups need super-admin approval** |

```
Path A: docs      → process blueprint → choose IDE → generate → review → export
Path B: interview → process blueprint → choose IDE → generate → review → export
```

The **process blueprint** (SIPOC, process flow, swimlane — rendered as PNGs) sits between
context and IDE: review it, ask for changes in plain language, and approve it. Generation
then follows the approved process. It is **optional** — skip it and the project generates
straight from the brief, exactly as before. See
[Process blueprint](#process-blueprint-sipoc-process-flow-swimlane).

| IDE | Zip contents |
| --- | --- |
| **Claude Code** | `.claude/agents` + `.claude/skills` + `.claude/rules/*.md` + **`CLAUDE.md`** + `backend/` (+ `frontend/` when UI) |
| **Cursor** | `.cursor/agents` + `.cursor/skills` + `.cursor/rules/*.mdc` + modular `backend/` / `frontend/` scaffold |
| **Windsurf** | `.windsurf/agents` + `.windsurf/skills` + `.windsurf/rules/*.md` + `AGENTS.md` + modular app scaffold |

Every export also gets a **`.gitignore`** written for its detected tech stack, at the
repo root beside `main.py`. See [Generated root files](#generated-root-files).

Every export includes a detailed **`README.md`** with:

- Modular **project structure** (starts at `main.py`; typically `backend/` + optional `frontend/`)
- Compact **file purposes** (one bullet per path — role/score of the file, no sparse gaps)
- Agents / skills / rules purposes
- How to use the workspace

**Example shapes** (adapted per brief — not hardcoded):

- Backend: `backend/api/routes|dependencies`, `services`, `repositories`, `models`, `schemas`, `core`, `utils`, `tests` (FastAPI + Pydantic + SQLAlchemy/SQLModel + Postgres + Redis when relevant)
- Frontend: `frontend/app|components|features|hooks|lib|services|types|tests` (Next.js + TypeScript when UI is implied)

Scaffold files include a **Role:** docstring/comment. Older `app/` trees are upgraded on export/preview.

---

## Prerequisites

- Python **3.11+**
- Node.js **22.22.3+** (UI only) — Angular 22's CLI refuses to run on anything older, and on
  odd-numbered majors; v24.15.0 and v26.0.0 also qualify. `apt install nodejs` on Ubuntu 22.04
  gives v18, which is **not** enough: `nvm install --lts && nvm alias default "lts/*"`. `npm start`
  says so and stops if the version is too low
- AWS Bedrock credentials for live generate (or use `--demo`)
- `matplotlib` (in `backend/requirements.txt`) renders the blueprint PNGs — it ships its own
  fonts and uses the headless **Agg** backend, so no display, no Graphviz `dot` binary and no
  Node/Chrome are needed on the server
- `reportlab` and `python-docx` (same file) write `SDD.md` as a PDF and as a Word document. Both
  are pure Python — no LibreOffice, no `wkhtmltopdf`, nothing to install outside the venv. They
  are imported lazily inside the writer, so a deploy that forgets `pip install` boots fine and
  then fails the first download instead

---

## Quick start — Windows `cmd.exe`

Two `cmd` windows, from the repo root. Ports come from `.env` (see §1).

```bat
REM ── window 1: backend on :8555 ────────────────────────────────
copy .env.example .env
cd backend
python -m venv .venv
.venv\Scripts\activate.bat
pip install -r requirements.txt
powershell -ExecutionPolicy Bypass -File run_api.ps1

REM ── window 2: UI on :4225 ─────────────────────────────────────
cd frontend
npm install
npm start
```

Then open http://localhost:4225 and log in with `admin@ac.com` / `1681149@sPk`.

For the CLI, a third window:

```bat
cd cli
python -m venv .venv
.venv\Scripts\activate.bat
pip install -e .
set PYTHONIOENCODING=utf-8
agentcraft health
agentcraft auth login
```

Full details, including PowerShell and macOS/Linux, in §2–§4 below.

---

## 1. Configure environment

From the **repo root**:

```bash
cp .env.example .env
```

On Windows `cmd.exe` that is `copy .env.example .env`.

Minimum `.env`:

```env
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_SESSION_TOKEN=...
AWS_REGION_NAME=us-east-1
LLM_MODEL=bedrock/us.anthropic.claude-sonnet-4-6
JWT_SECRET=change-me-to-a-long-random-string

# Ports — defaults shown; change them here and nothing else
API_PORT=8555
FRONTEND_PORT=4225

# Super admin (seeded on API startup)
SUPER_ADMIN_EMAIL=admin@ac.com
SUPER_ADMIN_PASSWORD=1681149@sPk
SUPER_ADMIN_NAME=Super Admin
```

Generation runs on **Claude Sonnet 4.6**, and falls back to **Claude Sonnet 5** when Sonnet 4.6 is
unavailable or throttled — a different model, because a second profile of the same one shares
whatever made the first unavailable. Both are set in `.env`:

```env
LLM_MODEL=bedrock/us.anthropic.claude-sonnet-4-6
LLM_FALLBACK_MODEL=bedrock/anthropic.claude-sonnet-5
```

**The two ids are shaped differently, and neither is a typo.** This is the one part of the
configuration where copying the pattern from the other line is the mistake:

| | Sonnet 4.6 | Sonnet 5 |
|---|---|---|
| Bedrock access | cross-region inference profile | `InvokeModel`, no profile exists |
| Routing prefix | **required** — `us.` / `global.` | **must be omitted** |
| Bare id | 400: *pass an inference profile* | correct |
| `LLM_TEMPERATURE` | honoured | **400** — the client omits it |

For Sonnet 4.6, a bare `bedrock/anthropic.claude-sonnet-4-6` comes back as a 400 telling you to
pass an inference profile; a profile id is the base id with a routing prefix in front of it, where
`bedrock/global.…` routes dynamically at no premium and `bedrock/us.…` (or `eu.`/`jp.`) guarantees
the region for a 10% premium. Its base id is dateless with no `-v1:0` suffix —
`anthropic.claude-sonnet-4-6` is the whole id.

Sonnet 5 has no ARN-versioned model id and no inference profile, so the bare id is the only one
that works. It also rejects `temperature`, `top_p` and `top_k` outright — `LLM_TEMPERATURE`
therefore applies to the primary only, and the client drops it for models listed in
`_NO_SAMPLING_MODELS` ([client.py](backend/app/services/llm/client.py)) rather than sending a value
that would fail every fallback call. Add a model there if you point `LLM_FALLBACK_MODEL` at another
of the current generation (Opus 5, Opus 4.7/4.8, Fable 5).

### When `AWS_SESSION_TOKEN` expires

Temporary credentials expire — typically within hours. The symptom is narrow and easy to
misread: signup, login, workspaces, admin and export all keep working, while anything that
calls Bedrock answers **502** with *"AWS Bedrock credentials expired or invalid"* — so the
app looks half-broken rather than unconfigured. `agentcraft docs add`, `interview expand`
and `generate` are the ones that fail.

Confirm it in one command, then refresh the three `AWS_*` values in `.env` and **restart the
API** (they are read at startup):

```bash
aws sts get-caller-identity      # ExpiredToken => refresh the credentials
```

### Ports are configured in one place

`API_PORT` (**8555**) and `FRONTEND_PORT` (**4225**) in the repo-root `.env` are the
single source of truth. Everything else is derived, so moving a port is a one-line edit:

| Consumer | How it picks the port up |
| --- | --- |
| Backend (`uvicorn`) | `backend/run_api.ps1` (Windows) / `backend/run_api.sh` (Linux, macOS) read `API_HOST` / `API_PORT` from `.env` |
| Backend CORS | `cors_origin_list` allows `localhost` **and** `127.0.0.1` on `FRONTEND_PORT` (separate origins to a browser), plus `PUBLIC_HOST`; `cors_origin_regex` allows any hostname on `FRONTEND_PORT` **or on the scheme's default port** (a UI behind a TLS proxy sends an Origin with no port at all), unless `API_CORS_STRICT=true` |
| Angular app | `scripts/write-env.cjs` bakes `API_PORT` into `src/environments/environment.ts` (the browser cannot read `.env`); the **host** is resolved from `window.location` at run time, so one build is portable |
| `ng serve` | `scripts/serve.cjs` passes `--port $FRONTEND_PORT`, `--host $FRONTEND_HOST` and `--allowed-hosts true` |
| CLI | `api_url()` reads `.env` itself and tries loopback on `$API_PORT` first, then `$BACKEND_URL`, then `$PUBLIC_HOST` — loopback leads because the CLI normally runs on the API's own box, and an EC2 instance cannot reach its own public address. `AGENTCRAFT_API_URL` pins it outright |
| Docker | `docker-compose.yml` maps `${API_PORT}`; the image's `CMD` expands it at run time |

Precedence everywhere is **real environment → `.env` → `.env.example` → built-in default**,
so a one-off `API_PORT=9001 npm start` works without editing a file, and a fresh clone with
no `.env` yet still builds against the documented ports.

Optional overrides, all commented out in `.env.example`:

- `PUBLIC_HOST` — pins the API host. **Not needed to deploy**: the UI resolves it from the
  browser. Set it only when the API is on a different host from the UI. See
  [The same setup runs locally and on EC2](#the-same-setup-runs-locally-and-on-ec2).
- `FRONTEND_HOST` — dev-server bind address, `0.0.0.0` by default (which serves localhost
  too). Set `127.0.0.1` to keep it strictly local.
- `API_CORS_STRICT` — `true` drops the any-hostname rule, leaving only the explicit
  allowlist. Recommended for anything internet-facing.
- `API_CORS_ORIGINS` — **adds** origins; it does not replace the automatic `FRONTEND_PORT`
  and `PUBLIC_HOST` entries, so changing either can't silently break the browser.
- `AGENTCRAFT_API_URL` — a full URL for an API that `PUBLIC_HOST` + `API_PORT` cannot
  express: behind a reverse proxy, on https, or under a path prefix.

`frontend/src/environments/environment.ts` is generated and **gitignored** — `.env` is the
source of truth, and committing it would let a stale port ship.

> **One `.env`, at the repo root.** There is deliberately no `backend/.env`. `Settings`
> pins `env_file` to an absolute path (`ROOT_ENV` in `app/core/config.py`), so the backend
> reads the same file whether it is started from `backend/`, from a test runner, or from a
> container. A second `.env` next to the app used to shadow the root one for any key the
> root left unset — which pinned CORS to a stale port and made the effective config depend
> on which code path happened to load it first. Put local overrides in the root `.env`, or
> pass them as real environment variables (they win over the file).

### The same setup runs locally and on EC2

**There is nothing to change between the two.** No `.env` edit, no rebuild for a specific
address. Start the stack the same way in both places:

| You open | UI served from | API called |
| --- | --- | --- |
| `http://localhost:4225` | your machine | `http://localhost:8555` |
| `http://44.194.125.199:4225` | EC2 | `http://44.194.125.199:8555` |

**How.** The Angular app reads its API host from `window.location` at run time
([api-base.ts](frontend/src/app/api-base.ts)) and keeps the page's own hostname and scheme,
substituting `API_PORT`. The backend accepts the UI on `FRONTEND_PORT` from any hostname,
so the origin check passes in both places.

This replaced a compiled-in `http://127.0.0.1:8555`, which could only ever be right in one
place: the bundle runs in the **visitor's** browser, so on a deployed box that URL pointed
at *their laptop* and every call died there — a UI that loaded fine, then "Could not load
sessions" and `ERR_CONNECTION_REFUSED` in the console.

Three things still have to line up, and all three now default correctly:

| Layer | Default | Would break if |
| --- | --- | --- |
| API bind address | `API_HOST=0.0.0.0` — all interfaces | `--host 127.0.0.1`: starts cleanly, accepts nothing from off-box |
| Dev-server bind | `0.0.0.0`, `--allowed-hosts true` | localhost-only bind, or Vite answering "Blocked request" to an unknown Host |
| CORS | any hostname on `FRONTEND_PORT`, or on 80/443 for a proxied UI | an allowlist that names only localhost — preflight rejected |
| API address in the browser | resolved from `window.location`; `FRONTEND_URL`/`BACKEND_URL` override it for that one address | assuming the API shares the UI's origin when it is on a second domain — every call 404s |

**The one thing outside this repo:** the EC2 **security group** must allow inbound TCP on
**both `4225` and `8555`** from your IP. The browser calls the API directly, so opening only
the UI port leaves every API call blocked — the exact same symptom as the bug above. To
expose a single port instead, put nginx in front and set `AGENTCRAFT_API_URL`.

Optional pinning, when the API is **not** on the same host as the UI:

```env
PUBLIC_HOST=203.0.113.10      # or AGENTCRAFT_API_URL for a proxied/https API
```

> **Two production caveats.** `ng serve` is a development server — fine for a demo or an
> internal box, not for real traffic; use `npm run build` and serve `dist/` from nginx.
> And CORS defaulting to any hostname is what makes one build portable; for anything
> internet-facing set `API_CORS_STRICT=true` and name your origins in `PUBLIC_HOST` /
> `API_CORS_ORIGINS`.

#### Behind a reverse proxy, with the UI and API on **different** domains

The deployed setup is two hostnames in front of the one tmux stack:

| | URL | proxied to |
| --- | --- | --- |
| UI | `https://agentcraft-poc.bpsgentech.com` | `FRONTEND_PORT` (4225) |
| API | `https://agent-craft-be.bpsgentech.com` | `API_PORT` (8555) |

This is the one shape the browser cannot work out for itself. `api-base.ts` reads the API host
from `window.location`, and a page served on 443 with no port in the URL means "there is a proxy
in front of me" — from which the reasonable guess is that the API is behind the *same* proxy, on
the same origin. Here it is not, so every call would 404 against the UI's own domain. Hence, in
`.env` (already set in `.env.example`, so a `git pull` is enough):

```env
FRONTEND_URL=https://agentcraft-poc.bpsgentech.com
BACKEND_URL=https://agent-craft-be.bpsgentech.com
```

Two keys, read by all three parts of the stack:

| | Uses |
| --- | --- |
| Angular | a browser on `FRONTEND_URL`'s host calls `BACKEND_URL` |
| API | `FRONTEND_URL`'s origin joins the CORS allowlist (path and trailing slash trimmed — CORS compares origins) |
| CLI | `BACKEND_URL` is tried after loopback, so `agentcraft` works from a laptop unconfigured |

**Why not `AGENTCRAFT_API_URL`:** that pins the API for every way of reaching the UI at once, so
it would also send `http://localhost:4225` and `http://<ec2-ip>:4225` — the addresses the tmux
stack is used through — out to the public backend. The pair above is *conditional*: the Angular
half applies only to a browser already on `FRONTEND_URL`'s host. That is why it is safe to commit
and why the existing access paths are untouched:

| Opened at | API it calls |
| --- | --- |
| `https://agentcraft-poc.bpsgentech.com` | `https://agent-craft-be.bpsgentech.com` |
| `http://localhost:4225` | `http://localhost:8555` |
| `http://<ec2-ip>:4225` | `http://<ec2-ip>:8555` |

Nothing else needs configuring. Specifically:

- **API routing** is unchanged — every endpoint stays under **`/api/v1/...`**, with `/health` and
  `/docs` at the root. `BACKEND_URL` is a bare origin, no path prefix, so the UI calls
  `https://agent-craft-be.bpsgentech.com/api/v1/...` and the target group's health check hits
  `/health`.
- **CORS** needs no separate entry: `FRONTEND_URL` is in the allowlist, and the default regex
  accepts an Origin with no port anyway. Verified — preflight from
  `https://agentcraft-poc.bpsgentech.com` returns **200** with a matching
  `access-control-allow-origin`.
- **Auth** has nothing URL-dependent: login is a `POST` returning a JWT that the UI keeps in
  `localStorage` and sends as `Authorization: Bearer`. There are no cookies, no redirect or
  callback URLs, and no OAuth/SSO provider to register a domain with. `JWT_SECRET` and
  `JWT_EXPIRE_HOURS` are unaffected by a change of hostname. (`/api/v1/auth/token` is OAuth2
  *password* flow — a form post, still no redirect.)
- **`PUBLIC_HOST` must stay unset.** It means "the API is at `http://<this-host>:<API_PORT>`",
  so setting it to the UI's hostname would make the app call
  `https://agentcraft-poc.bpsgentech.com:8555` — a port the ALB does not listen on. The pair
  above names the two halves separately, which is what a proxied deployment needs.

Two things worth knowing:

- The proxy must **forward websockets** on the UI host, or the dev server's live-reload socket
  fails in the console. The app itself still loads and works.
- The API must be reached over **https** from an https UI — an http API URL is blocked as mixed
  content before the request is ever sent.

### Running the CLI on EC2

Nothing to configure — the same `agentcraft` commands work on the instance as on a laptop:

```bash
cd ~/ST-V3/AgentCraft-HexaAgent/cli
source .venv/bin/activate
agentcraft health          # names the URL it resolved
agentcraft auth login --email admin@ac.com --password '...'
```

The CLI tries `127.0.0.1:$API_PORT`, then `localhost`, then `PUBLIC_HOST`, and uses the
first that answers `/health`. Loopback deliberately comes **first**: the CLI runs on the
same box as the API, and an EC2 instance cannot connect to its own public IP — traffic to
the elastic IP leaves for the internet gateway and is never routed back. A CLI that
preferred `PUBLIC_HOST` got `ConnectError: [Errno 111] Connection refused` against a
backend running perfectly well beside it.

So on EC2 the fix for "connection refused" is almost always **start the backend**, not
change a URL:

```bash
tmux ls                                # is an api session running at all?
curl http://127.0.0.1:8555/health      # {"status":"ok","service":"agentcraft"}
cd ../backend && ./run_api.sh          # if that failed — or see TMUX.md
```

On loopback the CLI says so outright — *"The AgentCraft backend is not running"* plus the
command to start it — because a refused connection there proves nothing is listening on
this machine. Against a remote URL the cause is genuinely unknown from the client, so it
lists the API, the security group and the URL to check.

If the backend keeps being down after a reboot or a crash, tmux is not enough — a session
dies with the machine. Install it as a service:

```bash
sudo ./deploy/install-service.sh        # restarts on crash, starts at boot
sudo systemctl status agentcraft-api
```

See [TMUX.md](TMUX.md#keeping-it-up-without-tmux-systemd). Use tmux **or** systemd, not
both, or they fight over the port.

Running the CLI **from your laptop** against the EC2 API is the one case that needs
configuration — loopback would be your own machine:

```bash
export AGENTCRAFT_API_URL=http://44.194.125.199:8555
```

That needs TCP 8555 open in the security group, and it sends your credentials over plain
http — fine for a demo, worth a proxy with TLS for anything else.

### Running under tmux (EC2, or any long-lived shell)

```bash
./run-tmux.sh                     # backend + UI in one detached session
tmux attach -t =agentcraft        # watch the logs
./run-tmux.sh stop                # stop both — only the `agentcraft` session
```

The `=` is not decoration: a bare `-t agentcraft` also matches any session whose name merely
*starts* with it, so on a box running several tmux stacks it can attach to — or kill — the wrong
one. The script uses exact targets throughout and lists what it left alone.

**Both panes restart themselves.** Each runs its service under `deploy/supervise.sh`, because
starting cleanly turned out not to be the same thing as still being up an hour later: the UI
pane would print `Terminated` and stop, which is SIGTERM from outside the process — on a small
box, a memory reaper taking the dev server, since it is by far the largest process there. The
supervisor brings it back and prints the signal by name, the uptime, the memory at that moment
and any out-of-memory record from the same window, so the cause is on screen instead of being
one unattributable word.

The reaper signals the **whole process group**, so the supervisor is hit alongside the service
it runs — which means a signal arriving there says nothing about intent. The signal does: SIGINT
(`Ctrl-c`) and SIGHUP (`./run-tmux.sh stop`, which closes the pane) stop for good, while SIGTERM
means something reaped it and the service comes back. One consequence: `kill <pid>` no longer
stops a pane, since TERM is its default — use `Ctrl-c` or `./run-tmux.sh stop`. Four failed
starts in a row stop it rather than burying a real build error, and `run-tmux.sh` warns up front
when the box has under 2 GB of RAM and no swap. See
[TMUX.md → When a pane says `Terminated`](TMUX.md#when-a-pane-says-terminated).

**See [TMUX.md](TMUX.md)** for the four layouts (script, one session per service, split panes,
detached one-liners) with start / attach / kill commands for each, a key cheat sheet, and how
to free a port that stays busy.

Four failures kill a pane within a second of starting and are **not** application bugs — all
are covered in [TMUX.md → Startup failures that look worse than they
are](TMUX.md#startup-failures-that-look-worse-than-they-are):

- **UI:** `Cannot find module '.../node_modules/@angular/cli/bin/ng.js'` — dependencies were
  never installed, or an install was interrupted. `cd frontend && npm ci`. `run-tmux.sh` now
  tests for that exact file and installs before starting, so a fresh clone comes up on its own.
- **UI:** `The Angular CLI requires a minimum Node.js version of v22.22.3` — the packages
  installed fine, but **Node is too old**. Angular 22 needs Node v22.22.3 / v24.15.0 / v26.0.0;
  Ubuntu 22.04's `apt` node is v18. `nvm install --lts && nvm use --lts && nvm alias default
  "lts/*"`, then `rm -rf node_modules && npm ci`. `npm start` now stops with these commands before
  Angular gets a chance to, `package.json` declares it in `engines`, and the Python API is
  unaffected. In tmux, `nvm use` only changes the shell that ran it — `exec bash` in the pane, or
  restart the session, after setting the default.
- **UI:** `Cannot find module '../rolldown-binding.linux-x64-gnu.node'`, thirty frames deep in
  `node_modules/vite/…` — the **sequel to the one above**. Vite's bundler is a compiled binary
  shipped as an *optional* dependency per platform, and npm skips optional deps whose `engines`
  do not match the Node running the install, so a tree installed on Node 18 has none of them.
  Upgrading Node does not repair it — npm sees a satisfied tree — so the install must be redone:
  `rm -rf node_modules && npm ci`. `npm start` now names this before Angular loads
  (`frontend/scripts/check-native.cjs`) and `run-tmux.sh` reinstalls the pane automatically.
- **API:** `could not translate host name "…rds.amazonaws.com" … Name or service not known`,
  then `Application startup failed` — `DATABASE_URL` points at a Postgres host this machine
  cannot resolve, usually one left in `.env` by another project on the same box. Set
  `DATABASE_URL=sqlite:///./agentcraft.db` (the shipped default, no server needed) and restart.
  The API now reports this in four lines naming the host and the fix, password stripped, rather
  than as a SQLAlchemy traceback.

---

## 2. Start the backend (required for UI + CLI)

```bash
cd backend
python -m venv .venv
```

**Windows PowerShell:**

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
.\run_api.ps1          # host + port from .env; prints the URL it binds
```

**Windows `cmd.exe`:**

`run_api.ps1` is PowerShell, so from `cmd` either call PowerShell to run it, or pass the
port on the command line:

```bat
.venv\Scripts\activate.bat
pip install -r requirements.txt

REM Option 1 — reuse the script, so the port still comes from .env
powershell -ExecutionPolicy Bypass -File run_api.ps1

REM Option 2 — plain cmd. Set the port here to match API_PORT in .env
set PYTHONPATH=.
set API_PORT=8555
.venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 0.0.0.0 --port %API_PORT%
```

Option 1 is preferred: it reads `.env` itself, so changing `API_PORT` there is enough.
Option 2 needs the `set API_PORT=` line kept in step with `.env` by hand.

**macOS / Linux:**

```bash
source .venv/bin/activate
pip install -r requirements.txt
./run_api.sh           # host + port from .env; prints the URL it binds
```

Or by hand — note `--host`, not just `--port`:

```bash
export PYTHONPATH=.
uvicorn app.main:app --reload --host "${API_HOST:-0.0.0.0}" --port "${API_PORT:-8555}"
```

> **`--host 127.0.0.1` binds loopback only.** It works on your own machine and refuses
> every connection from anywhere else, which on a server looks like a healthy startup log
> and a browser that cannot connect. Use `0.0.0.0` (the `API_HOST` default) when the API
> has to be reachable off the box. `run_api.sh` does this for you.

- Health: http://127.0.0.1:8555/health  
- OpenAPI: http://127.0.0.1:8555/docs  

On startup the API seeds the super admin if missing. Both URLs follow `API_PORT` — if you
changed it, substitute your own port. Deploying to a server? See
[Deploying to EC2 or any remote host](#deploying-to-ec2-or-any-remote-host).

### Trying endpoints from Swagger UI

The docs page has an **Authorize** button: everything except `signup`, `login`, `token`,
`meta/platforms` and `/health` is padlocked and needs a token.

1. **Authorize** → enter the account's email as **username**, plus its password. Leave
   `client_id` and `client_secret` blank — this API has no registered clients.
2. **Authorize** again to submit. Swagger signs in through `POST /api/v1/auth/token` (the
   OAuth2 password flow) and holds the token itself; there is nothing to copy by hand.
3. Padlocked endpoints now run as that user for the rest of the session.

`/auth/token` is form-encoded and returns only `{access_token, token_type}`, because that is
what the OAuth2 dialog expects. From code keep using `POST /api/v1/auth/login`, which takes
JSON and returns the user object with the token — that is what the UI and CLI call.

The padlock means "needs a token", not "you may call it". Role checks still apply on top:
the **admin** section and `PATCH /settings/llm` answer `403 Admin access required` for a
normal user, and `projects` endpoints only ever return the caller's own workspaces.

---

## 3. Angular UI

```bash
cd frontend
npm install
npm start
```

On **Windows `cmd.exe`** the commands are identical — npm scripts run the same way:

```bat
cd frontend
npm install
npm start
```

Open http://localhost:4225

`npm start` prints the ports it resolved from `.env` before Angular builds, so a mismatch
is visible immediately:

```
[env] environment.ts -> API http://127.0.0.1:8555
[env] ng serve on :4225 -> API http://127.0.0.1:8555
```

### How wide the app gets

One number, in `frontend/src/styles.css`:

```css
--shell-max: 1600px;                       /* content width cap */
--shell-pad: clamp(1.25rem, 2.4vw, 2.25rem);   /* gutter either side */
--read-max: 112ch;                         /* longest line of prose */
```

The wizard, the session board and the admin console all read `--shell-max`, so they line up
when you move between them; change it in one place to change all three. It is a **cap, not a
target** — past roughly 1600 px a full-bleed layout stops helping, because the eye travels
further between a row's label and its value than it saves in scrolling.

Widening the shell is not the same as widening everything inside it, and the difference is
where a wide layout usually goes wrong:

- **Panes that hold something get the space.** The Review and Export file browsers put the
  tree in a fixed 320 px band and give every remaining pixel to the file — a percentage
  column would have grown into 500 px of padded 24-character filenames. Tree, editor and
  Markdown preview are all sized `min(64vh, 680px)`, so a 137-file workspace is a list you
  can see rather than six rows and a scrollbar, and a PNG from `docs/diagrams/` or
  `docs/architecture/` opened in that pane is fitted to ~1200 px instead of ~700.
- **Paragraphs keep a measure.** Running prose is bounded by `--read-max`, not by the card
  it sits in: a 200-character line loses the reader on the return sweep to the next one.
  Code blocks, tables and file contents are deliberately exempt — wrapping those to 112
  characters hides the thing you opened the file to check.

### Super admin (UI)

1. Log in with `admin@ac.com` / `1681149@sPk`
2. You land on **`/admin`**
3. See Pending / Approved / Rejected / **Deleted**, expand users → sessions
4. Click a session to see, read-only:
   - **which context pipeline it followed** — a `Documents` / `Interview` / `Path not chosen`
     badge on the row, and a sentence saying what came in (documents uploaded, or interview
     questions answered). A badge marked `?` was *deduced* from the brief rather than read
     off the row: sessions created before the path was recorded still took one, and calling
     those "not chosen" would be wrong
   - **agent / skill / rule / document names** (names only)
   - **the blueprint the user approved** — the three views (SIPOC, process flow, swimlane) in
     the same viewer the user has: tabs, zoom, **Fit**, **100%**, drag-to-pan, **Full screen**,
     Open / Download PNG, and the keyboard shortcuts. Fetched only when you open the session,
     because the PNGs are megabytes each at 3× supersampling
5. **Approve** or **Reject** pending signups
6. **Delete user** — revokes any non-admin account (approved ones included) after an
   in-app confirmation. The card moves to the **Deleted** tab, where **Restore user**
   undoes it. See *Deleting and restoring a user* below
7. Create your own workspaces anytime:
   - **New workspace** → opens the wizard
   - **My sessions** → your sessions list (same studio as regular users)
8. From Sessions / Wizard, use **Admin** to return to the control room

Other users’ workspaces stay **view-only** in `/admin`: names, the pipeline they followed, and
their diagrams — no opening, editing, regenerating or downloading of their projects. The
diagrams come from two admin-gated read-only routes
(`GET /api/v1/admin/projects/{id}/diagrams` and `…/diagrams/{kind}.png`) rather than the
studio's own, because every `/projects/**` handler filters by owner and returns `404` for
someone else's project. A non-admin calling them gets `403`.

The blueprint panel also shows **what each follow-up was allowed to change**, because a version
number says how often someone came back, not what they came back for — and a change that rewrote
the process moved the generated agents with it, while a change scoped to one view did not:

- The collapsed session row's blueprint chip ends with the last follow-up's scope —
  `Blueprint v3 · draft · SIPOC only`
- The expanded panel carries a read-only **Change history** under the viewer: a sentence
  summarising the follow-ups, then one row per version with the instruction in quotes, the
  model's summary, and a green **All three views** / violet **\<view\> only** badge
- **The rows are clickable** — they load that version's three PNGs into the same viewer, with a
  banner naming the version and one click back to the current one. Same on-demand render as the
  wizard's history: only the last **8** versions keep the views needed to redraw them, so an
  older row stays as a record and says *images not kept*. Still read-only — an admin can look at
  any version, and can never redraw, revise or approve one

### Deleting and restoring a user (super admin)

**Delete user** is available on every non-admin card — `pending`, `approved`, and
`rejected` alike.

Deleting is a two-stage process, so a mistake is recoverable:

| Stage | Effect | Reversible? |
| --- | --- | --- |
| **Delete user** | The `users` row is archived. Login stops working immediately and open tabs are signed out. Their workspaces are **kept**, still owned by the archived id | **Yes** — Restore user |
| **Erase permanently** | The archive **and** every held workspace are deleted for good | No |

- The card moves to the **Deleted** tab (its own KPI and filter chip) showing when it was
  deleted, by which admin, and the status it held
- **Restore user** brings back the account with its **original password**, its previous
  status, and every workspace reattached — the password hash was archived, never reset. A
  restored `pending` user is still `pending`; restore never grants access they never had
- **Erase permanently** is the only action that destroys data. After it, restore is
  impossible and the person gets the ordinary `401` instead of a deletion notice
- **The super admin account cannot be deleted** — the API rejects it with `400`, so you
  cannot lock yourself out

**What the deleted person sees.** The archive row (`deleted_users`) records the removal,
so instead of a misleading “invalid email or password” they get:

> Your account was deleted by the admin. You no longer have access to AgentCraft — sign
> up again or contact support.

- **On login** → `403` with that message
- **With a tab still open** → their JWT decodes but the next API call returns `403`; the
  UI logs them out and shows the message on the login screen
- **Signing up again** supersedes the archive — the new account is simply `pending`, and
  the old id's held workspaces are dropped (that id can never be restored, so nothing
  could ever reach them)
- Unknown emails still get the generic `401`, so the message never reveals who existed

Existing databases upgrade themselves: the archive gained the columns restore needs
(`password_hash`, `role`, `status`, `user_created_at`) via an automatic `ALTER TABLE` on
API startup. Accounts deleted *before* that upgrade show **not restorable** and can only
be erased — those people must sign up again.

CLI parity: `agentcraft admin delete | restore | purge <user-id>` (see §5). A CLI whose
account was deleted prints the same message and drops the stale `~/.agentcraft/token`.

### New user (UI)

1. **Sign up** → Name, Email, **Password**, **Confirm password**
2. Account is **pending** (no dashboard access)
3. Super admin approves
4. User **Log in** → **Sessions** → New workspace / wizard / export  
   Press **Enter** in login/signup forms to submit.

### Uploaded documents (Path A)

Attaching files gives feedback at every stage — nothing looks frozen:

| Stage | What you see |
| --- | --- |
| Browsing / dropping files | Dropzone switches to a **spinner** — “Adding N files… reading from your device”; Browse, Back and Continue disable |
| Attached | One chip per file (type pill, name, size) + “**N files attached and ready to upload**” |
| Removing one | The chip’s **×** drops it instantly; **Remove all** clears the lot when more than one is attached |
| Unsupported or unreadable | Named in a warning — wrong extension, empty, moved, or still syncing from cloud storage |
| Continue (upload) | Full-card overlay with elapsed seconds and the current phase (extracting text → semantic match check) |
| Saved | **Already saved with this project** list, which survives Back / reload / a new session |

Each file is probed with a one-byte read while the spinner is up. Opening a handle is
where slow disks, network shares, and un-hydrated OneDrive files actually stall, so a file
that cannot be read is reported by name instead of failing later mid-upload.

Chips are tracked by `File` object rather than list index, so removing one detaches a
single DOM node instead of re-creating every chip after it — which is what used to make
the **×** feel slow.

Uploads are recorded in SQLite alongside the project, so you can always see what a
workspace was built from:

- **Wizard → Documents step** lists the documents already saved with the project
  (filename, type, size, extracted characters)
- **Sessions → Details** shows **Documents** chips per workspace
- **CLI**: `agentcraft docs list`, plus a `Documents` row in `status` and a
  `Documents` column in `sessions`

Storage details:

- Records live in the existing `brief_json` column (`ProjectBrief.documents`) — **no
  schema migration** is needed, and rows written before this feature still load
- Extracted **text** is what generation uses; the record exists so the source file is
  attributable and re-listable
- **Existing projects are upgraded automatically.** On API startup an idempotent
  backfill reconstructs the document list from the `### filename` headers already in
  `document_text`. Original byte sizes were never recorded for those uploads, so the
  size is hidden rather than guessed
- Re-uploading **replaces** the list — one project shows one set of documents
- Browsers do not expose local file paths, so only the filename is stored

### Process blueprint (SIPOC, process flow, swimlane)

Between **Context** and **IDE** the wizard now has a **Blueprint** step. It reads the
problem statement *and* the extracted text of every uploaded document, then draws three
views of the same process:

| View | Answers |
| --- | --- |
| **SIPOC** | Who supplies what, the high-level process, what comes out, and for whom |
| **Process flow** | Every step, decision, loop-back and end state, in order |
| **Swimlane** | The same steps, split by the actor who performs them |

**Each view says what it is for, in the UI and in the CLI.** The names describe the notation,
not the question — a reader meeting SIPOC for the first time cannot tell from "suppliers,
inputs, process, outputs, customers" why they are being shown it. So the empty state's three
tiles, and a line under the tab strip once the views exist, carry the plain-language purpose
(*scope on one page* / *the order of work* / *who owns what*); the tab's tooltip is the same
sentence, and `diagrams show`, `diagrams generate`, `diagrams refine` and `diagrams save`
print the same three lines as a legend above their output.

**They are three projections of one extracted process model, not three separate answers.**
That is the whole reason they agree: a step in the swimlane is always in the flow, and the
SIPOC process column is always a compression of the same steps. Independent prompts per
diagram produced diagrams that contradicted each other.

How a blueprint is produced:

1. **One extraction pass** reads the brief into a `ProcessModel` — actors, steps, edges,
   data objects, KPIs, assumptions
2. **Three specialised passes run in parallel** (`asyncio.gather`), one per view, each
   given the shared model
3. **Reconciliation** repairs what came back against the model — dangling edges dropped,
   missing steps added back, invented steps removed, empty SIPOC columns filled, lanes
   rebuilt from each step's actor. Every repair is **recorded as a warning** and shown in
   the UI rather than hidden
4. **Deterministic rendering** turns the JSON into PNGs

**No image model is involved, and that is deliberate.** A SIPOC or swimlane is worth
nothing unless every label is exactly right and every handoff arrow lands on the correct
box — which is precisely what diffusion models cannot guarantee. Claude emits structured
JSON; `matplotlib` (Agg backend, no display needed) draws it. Every edge label is placed by
searching along its own arrow and in bands either side of it, in order of what a bad spot costs
the reader: clear of every box and label, else clear of every *string* on the page (so the plate
covers a box's padding rather than its words), else further out. A label's plate is opaque, so a
label on a box costs that box's name and a label on a label costs both. Distance is bounded the
same way — a label far enough from its arrow belongs to no arrow at all — and past that bound
the attachment is drawn as a **dotted leader** rather than left to proximity.

**Every handoff is an orthogonal route through a reserved channel.** This is what fixed the
swimlane: a curved arrow drawn straight from box to box is fine with six steps and is an
unreadable tangle with twenty-five. The layout now leaves a **36 px gutter on every column
boundary** and **32 px above and below every row of boxes**, and nothing is ever drawn in
them except connectors. A handoff that is not to the very next stage therefore leaves its
box sideways, runs vertically inside a column gutter, crosses along the *target lane's*
boundary, and enters the target from the side — straight runs and right angles only, so a
single line can be followed by eye from end to end. Parallel routes in one channel are
stacked at fixed offsets (`_V_OFFSETS` / `_H_OFFSETS`) instead of overlapping, a vertical
run that would pass through an unrelated box in its own column is detected and pushed into
the gutter, and each diagram carries a **legend** for what its line styles mean:

| Line | Means |
| --- | --- |
| Solid, heavier | The next step / the next stage — the spine of the process |
| Solid, lighter | Skips ahead (a branch that jumps a stage) |
| Dashed | Sent back — a loop or rework path |

The geometry is a pure function, `swimlane_route(start, end, channel_x1, channel_x2,
channel_y)`, split out of the drawing so a test can assert the corner list misses the box
rectangles rather than a human having to squint at a PNG.

More is drawn from the model than before, so the picture answers questions that used to
need the step list: flow boxes carry the step's **description** and the **system** that runs
it under the name, with the actor and the step id captioned above; swimlane lanes carry the
actor's **kind and responsibilities** and every box its step id; the swimlane's top band
names each **stage** (`STAGE 3` plus what stage 3 *is*); SIPOC numbers its process column
and lists the process's **records** and **measures** underneath.

A swimlane is capped at **`MAX_STAGES = 10` columns** (`build.py`). One column per step is
the flow diagram redrawn sideways — wider than any screen, and it hides the phases that are
the whole point of the view. The layout prompt asks for at most ten phase columns, and
`_bucket_columns` merges adjacent columns as a backstop when a call ignores it or the
deterministic fallback derived them.

**The header is a band, not a caption.** Title, view badge and scope sit on a tinted strip
with a hairline rule under it and a 5px accent bar down its left edge — blue for SIPOC, green
for the flow, purple for the swimlane. Clear space alone was not enough: a paragraph of grey
text on the same white as the boxes reads as *part of the picture*, so a long scope looked
like a caption that had collided with the first step. The badge yields to the title when the
two cannot both fit — the title has to be readable in full, and the accent colour already
says which view this is.

**Nothing is cut to a character count.** The process **scope** under the title wraps to as
many lines as the canvas needs (up to four), and `_header_height` measures those same lines
to decide where the body starts — so a two-line scope pushes the whole diagram down instead
of being drawn over the first row. `_header_band` measures them separately, because the flow
view leaves extra room under the rule for its step captions and the rule must not move
because of it.

**The title wraps too** — to a second line, and only then is it ellipsised. Dropping the badge
was not always enough: a project named after its own one-line summary is wider than the flow
canvas on its own, and the title is the one line that says *which project this is*, so an
ellipsis there costs more than anywhere else. Only the first line leaves room for the badge; the
second gets the whole canvas, and a lone `—` at a break travels down with the words it joins
rather than hanging off the end of a heading. `_title_lines` is the single measurement — `header`
draws from it and `_header_band` / `_header_height` measure from it, so a two-line title moves the
band, the rule and every box on the picture together. Where a label genuinely has to fit a fixed space — a
step's actor on the top edge of its box, an edge label on the plate between two steps —
`_clip` shortens it *by measured width* and appends an ellipsis. A `label[:24]` slice is a
guess about how wide 24 characters are and it is wrong both ways: it cut
`Compliance Officer (second line)` mid-word with no sign anything was missing, while letting
a short but wide all-caps label overflow anyway.

`build.py`'s `_cap` bounds what goes *into* the stored model — a backstop so one runaway
sentence cannot become a box the size of the page — and it too breaks on a word boundary and
marks the cut. A label can therefore be shortened twice on its way to the page, so
`_ellipsize` guarantees exactly one "…": `current……` reads as a rendering bug rather than as
a sentence that continues. The full text is always in the step list beside the picture.

**Which cuts are real is audited, not eyeballed.**
`backend/scripts/_truncation_audit.py` draws all six views from a real generated project and
watches the three separate ways a sentence can be lost:

1. **The renderer shortened it.** `_wrap` and `_clip` are wrapped, and every string either of them
   shortened is printed with its call site. The distinction the audit exists to draw: a
   multi-sentence *description* bounded on a word boundary is the design working, while a
   **label**, a box note, or a `·`-joined list losing items is a defect — those heights are all
   measured from the wrapped text, so the fix is to raise the bound or give the box more room, not
   to accept the ellipsis.
2. **Something upstream shortened it first.** `_wrap` is blind to that one: the text handed to it
   already ends in somebody else's ellipsis, so the wrap looks clean. `architecture._s`,
   `architecture._prose` and `solution_design._text` are watched directly, compared on text with
   the markdown stripped so a rewrite is not mistaken for a cut. A `_s(scaling, 90)` had been
   turning "fails over automatically across AZs" into "fails over automatically" — the clause
   naming the failover domain, which is the answer.
3. **Something was drawn over it.** An edge label sits on an opaque plate, so a label on a label
   loses *both* and a plate through a box's sentence reads exactly like a truncated one. Every
   placed label is checked against every string on the page, and against how far it drifted from
   the arrow it annotates.

Only (1) is a judgement call, so only (1) is informational; (2), (3) and any stored prose ending
mid-sentence fail the run. Current state, which is the one to keep it in: **0 cuts, 0 labels over
text, 0 labels adrift** across all six views. Run it after any renderer change, and bump the
revision when you make one.

**Legibility comes from resolution, not bigger type.** Every coordinate in the renderer is
a *layout* pixel at 100 DPI — the size the diagram is meant to be read at — and the PNG is
saved at `DPI × RENDER_SCALE` (currently **3×**), so the same layout is backed by nine
times the pixels. A 25-step swimlane is ~2800 × 1950 layout pixels and ships as an
8406 × 5832 PNG; zooming to 300% still shows type rendered from real pixels rather than
an upscale. Growing the fonts instead would make each diagram physically larger
without making any of it more readable once the browser scales it to fit. The API reports
`scale` per image, and the viewer divides by it — that is what makes **100%** mean "the
size the labels were laid out for" rather than three times it. Agg refuses a figure over
65 536 px on a side, so `to_png` steps the scale down for a runaway layout instead of
failing the render.

`RENDER_REVISION` is part of each cached filename
(`<kind>-v<version>-r<revision>-s<scale>.png`). Bumping it when the drawing changes
re-renders **existing** projects on their next request, with no cache to prune by hand.

Working with a blueprint in the UI:

- **SIPOC is the landing tab.** It is the first view in the method and the one that fits a
  pane whole, so it is what orients someone who has just watched three diagrams appear; a
  redraw returns to it for the same reason
- Three tabs, each in a real viewer: **−/+** zoom, **Fit**, **100%**, **Full screen** (Esc to
  leave), **Open PNG** in a tab, **Download PNG**. Drag to pan, Ctrl/⌘ + wheel to zoom,
  double-click to toggle 100%. The footer shows the diagram's layout size
- **Fit in the page, 100% in full screen.** All three views open fitted, whatever their shape:
  the first thing you need from a diagram you have not seen is its size and shape, and that is
  what tells you where to zoom. **Full screen** is the explicit "now let me read it" gesture, so
  it goes to 100% — the size the labels were laid out for — and leaving it returns to fit.
  Switching tabs inside full screen stays at 100%. The transition is driven by the browser's
  `fullscreenchange`, so Esc, F11 and the window chrome all land on the same zoom as the button
- **Keyboard, on the blueprint step:** <kbd>+</kbd> / <kbd>−</kbd> zoom, <kbd>0</kbd> fit,
  <kbd>1</kbd> 100%, <kbd>f</kbd> full screen, <kbd>←</kbd> / <kbd>→</kbd> switch view.
  Bare keys, so they are ignored while the follow-up box or the step filter has focus, and
  modified keys are left to the browser (Ctrl+0 still resets page zoom)
- **Full screen** uses the browser's own fullscreen, so the viewer covers the whole screen
  rather than the card it lives in, and it enters on **Fit** — bounded by height as well as
  width, so the entire diagram is on screen at the largest size that fits, which is the point
  of going fullscreen. Zoom from there
- **All steps and actors, in full** — an expandable list under the diagrams with every
  step's name, id, actor, systems and description, untruncated, with a **filter box** that
  matches on name, id, actor, system or detail (a 25-step process is a lot to read to find
  one step). The renderer ellipsises a long label to keep a box readable; this is where the
  model's own wording lives, and it is the text a follow-up should be checked against,
  because it is what generation reads
- **Change something** — one plain-language instruction ("the reviewer also checks credit
  history", "drop the manual re-key step"). Every instruction and the model's summary of
  what it changed are kept as **change history**
- **Every follow-up asks what it may change, before it runs.** Two cards above the Apply
  button, and the answer is on screen while it works — the spinner names the scope and says
  whether the process is being revised:
  - **All three diagrams** *(default)* — the **process model** is revised and all three views
    are redrawn from it, so they never drift apart. This is the only way to change what the
    process *is*, and generation follows it: the model feeds the agents, skills and rules
  - **Only this diagram** — the view you are looking at is **re-laid out** with your
    instruction as presentation guidance (grouping, ordering, wording, level of detail, which
    phases the swimlane's columns represent). The **process model is not touched**, and neither
    are the other two views, so nothing can end up contradicting anything. The answer is
    reconciled against the *unchanged* model, so a layout that tries to invent or drop a step
    has it repaired and says so under **repairs and gaps**. Generated agents are unaffected
  - The choice follows the tab strip, so "Only SIPOC" on the SIPOC tab becomes "Only
    Swimlane" when you switch — it is always about the diagram in front of you
  - Either kind **reopens** an approved blueprint: what was approved was a blueprint the user
    was looking at, and both change what they would be looking at
  - After a view-only change the wizard returns you to the view that changed, not to SIPOC
  - Each change-history entry carries a badge — green **All three views**, violet
    **SIPOC/Process flow/Swimlane only** — so months later the log still says which changes
    moved the process and which only tidied a picture
  - Only the two views that did not change are reused: their PNGs are copied from the previous
    version rather than redrawn, which is why a single-view follow-up returns in a fraction of
    the time. The decision is made by comparing the stored views, not by trusting the
    requested scope, so a mismatch cannot serve a stale image
- **A follow-up changes the content, not just the shape.** "Only this diagram" is a restriction
  on *what the process is*, never on the wording of the picture. On the view being redrawn, the
  instruction may rewrite labels, notes, stage names, edge labels and the title — naming
  conventions, capitalisation, tense, singular vs plural, level of detail, grouping — and on a
  SIPOC it may re-word, re-group, add or drop entries in any column, because a SIPOC's
  suppliers, inputs, phases, outputs, customers and metrics exist only on that view and nothing
  else draws them. What it may not move is the process itself: which steps exist, their ids and
  owners, and which leads to which. So *"change the naming convention for suppliers and
  customers"* is applied — under "only SIPOC" — rather than answered
- **A follow-up that changes nothing is refused, not recorded.** If the redrawn view comes back
  identical to the one on screen, the change is asked for a second time with the model's own
  non-answer quoted back at it. If that is identical too, **no version is saved**: the blueprint
  stays on the version being looked at, stays approved if it was approved, and the wizard shows
  an amber note (not a red error) suggesting how to say it more concretely. **Your instruction
  stays in the box** so a word can be added to it — the box is cleared only once a version
  actually lands. A v2 that promises a change and draws the same picture is the one outcome
  worse than being told it did not work
- **Change history entries are clickable, and show that version's diagrams.** Clicking `v1`
  loads the three PNGs as they were at v1 into the same viewer, with a banner naming the
  version and one click back to the current one. What is stored per superseded version is
  the three *views* (a few KB of JSON), not the images — so an old version is re-rendered on
  request by today's renderer, and the current set is never unloaded, making "back to
  latest" instant. The last **8** versions keep their views (`HISTORY_LIMIT`); older entries
  stay in the list as a record of what changed and say *diagrams not kept*
- **Approve blueprint** freezes it. **A follow-up always reopens it** — the frozen model is
  what generation reads, so it can never differ from the diagrams on screen
- **Assumptions**, **repairs and gaps**, and **measures** are listed under the image
- A **Placeholder** badge appears when the deterministic fallback drew them (demo mode, or
  Bedrock unavailable) instead of the model

The **freeze gate**: `POST /generate` returns **409** while a project has a blueprint that
is not approved. A project **without** a blueprint is untouched — no gate, no extra prompt
context, generation behaves exactly as it did before this existed. **Discard** removes the
blueprint and returns the project to that plain flow.

What generation actually receives: the approved process is prepended to the brief as
`Approved process blueprint`, ahead of the raw document text. If the length caps have to
drop something it is the tail of the brief, not the process the user explicitly signed off.

In the zip — **the approved version's three PNGs and nothing else**:

| Path | Why |
| --- | --- |
| `docs/diagrams/sipoc-v2.png` | The render of the version that was approved |
| `docs/diagrams/process-flow-v2.png` | Same version, so the three cannot be mixed |
| `docs/diagrams/swimlane-v2.png` | Same version |

`-v2` is whatever version the user approved. The version is **in the filename** for a
reason: a re-export after a revision writes `-v3` beside it rather than silently replacing
a set someone has already reviewed, and the number in the name is the number in the change
history. `agentcraft diagrams save` writes **the same filenames**, so a file on a laptop and
a file in the zip are comparable by name.

There is no `README.md` and no `process-model.json` inside `docs/diagrams/` — the folder
holds images only. What each image is for is documented in the **workspace's own
`README.md`**, which gains a `## Process blueprint (docs/diagrams/)` section: a tree of the
folder, one bullet per image saying what question it answers, the scope line from the SIPOC,
the assumptions that were accepted, and the list of blueprint revisions. That way the
explanation is in the file a developer opens first, not in a second README two folders down.

Everywhere the images appear:

| Where | What you get |
| --- | --- |
| **Review → Diagrams** tab (beside Agents/Skills/Rules/MCP) | **All six** — the three blueprint views in the same viewer as the Blueprint step, then a second viewer under an **Architecture — from `SDD.md` §3.2** heading for the three architectural views. Zoom, Fit, 100%, drag-to-pan, full screen, ← → to switch view, Open/Download PNG. Loaded only when the tab is opened |
| **Review → Files**, and the Export step's file tree | The `docs/diagrams/*.png` rows are listed with every other workspace file. Clicking one **opens the image in the viewer** instead of an empty editor; Copy / Edit / Save are hidden, because a render has nothing to edit |
| The **zip** | The same bytes, at the paths above |

The tab's count is the number of pictures it will show. It used to read **Diagrams (6)** and
render three: the architecture views were counted in `previewImages` but reachable only by
opening a `docs/architecture/` row in the Files pane. The two sets are now two labelled groups in
one tab, and the heading names their source — a reader's first question about a diagram is which
document it belongs to. Under the architecture group, each filename is a chip that opens that
picture, `SDD.md` is a chip that **selects the file** (same tab, no navigation away), and the two
document downloads sit on the line below.

The preview endpoint lists those paths in `files` and repeats them in a separate `images`
array, but never puts them in `contents`: the preview is polled every 4 s while
`WORKBREAKDOWN.md` generates, and three 3× renders are megabytes. The viewer fetches the one
it needs from the diagrams endpoint.

**The second picture folder: `docs/architecture/`.** A workspace exports *two* sets of PNGs
from the same renderer, and they answer different questions — keeping them in separate folders
is what stops them being confused:

| Folder | Three images | Answers | Versioned? |
| --- | --- | --- | --- |
| `docs/diagrams/` | `sipoc`, `process-flow`, `swimlane` | The **business process** the user approved | Yes — `-v2` in the filename, because an approved revision must not be overwritten |
| `docs/architecture/` | `logical-view`, `development-view`, `deployment-view` | The **software design**, embedded in `SDD.md` §3.2 | No — these draw the *current* design, cached by a digest of it |

They also cache in separate folders (`diagrams/<id>/*.png` versus
`diagrams/<id>/architecture/*.png`) for a duller reason worth knowing before touching either:
blueprint pruning globs `*.png` non-recursively and deletes anything without a
`-v{version}-r{revision}-s` marker in its name, which the digest-named architecture files
don't have. The subfolder is the fix. Both sets are listed the same way — in `images` and not
in `contents`, opened in the file pane as pictures with Copy / Edit / Save hidden, and printed
by the CLI as a `Diagrams:` line per folder, since `preview -f` cannot show a PNG.

**Both folders are documented in the generated workspace's own `README.md`.** `docs/diagrams/`
had a section and `docs/architecture/` did not, so a developer unzipping the folder found three
PNGs nobody had introduced. The exporter now splices an **Architecture (`docs/architecture/`)**
section in as well — a tree fence, one bullet per view naming its SDD section and what it is
for, why those filenames carry no version when the blueprint's do, and a closing paragraph
contrasting the two folders: the process first, then the system built to run it. It lists only
the views actually written, and lands above the `Generated for …` footer so that stays last.
`export()` and `preview_files()` both go through it, so the preview pane and the zip agree.

Storage and existing projects:

- The set lives in a new `projects.diagrams_json` column. On **SQLite** the startup
  migration adds it automatically. On **Postgres** run it yourself once:
  `ALTER TABLE projects ADD COLUMN diagrams_json TEXT;`
- PNGs are a **cache** under `diagrams/<project-id>/`, a pure function of the stored set —
  deleting that folder is safe, the next request re-renders. A renderer change bumps
  `RENDER_REVISION`, which changes the filename, so old projects redraw by themselves
- The same is true of a **history** version: asking for `?version=1` renders it from the
  views stored with that revision and caches it under `<kind>-v1-r<rev>-s<scale>.png`. The
  cache is pruned to the versions the set can still draw, so history never accumulates
  files for versions that have aged out
- Drawing three views at 3× is a few seconds of CPU (~10–16 s for a 25-step process), which
  is why generation runs as a **background job** the wizard polls rather than blocking a
  request
- Existing projects read as "no blueprint", which is a normal state — they keep generating
  exactly as they do today until someone draws one

Endpoints (all require auth and ownership):

| Method | Path |
| --- | --- |
| `POST` | `/api/v1/projects/{id}/diagrams?demo=&wait=` |
| `GET` | `/api/v1/projects/{id}/diagrams` (**null**, not 404, when there is none) |
| `GET` | `/api/v1/projects/{id}/diagrams/{sipoc\|flow\|swimlane}.png?version=` — `version` draws an earlier one from the change history (**404** once it has aged out); omit it for the current one |
| `POST` | `/api/v1/projects/{id}/diagrams/followup?wait=` — body `{instruction, scope}`; `scope` is `all` (default — revise the process, redraw all three views) or `sipoc`/`flow`/`swimlane` (re-lay out that one view, process untouched). **409** while the blueprint is frozen, whichever scope. **422** when the follow-up changed nothing after two attempts — no version is recorded and the message says how to be more concrete; its first words are `Nothing changed`, which is how the wizard and CLI know to show it as advice rather than a failure. On `wait=false` the same text lands in the project's `error` field |
| `POST` | `/api/v1/projects/{id}/diagrams/freeze` · `/unfreeze` |
| `DELETE` | `/api/v1/projects/{id}/diagrams` |

Three read-only twins exist for the super admin, gated on the admin role instead of ownership —
the ownership filter above returns `404` for someone else's project, which is right for the
studio and useless for the person reviewing it:

| Method | Path |
| --- | --- |
| `GET` | `/api/v1/admin/projects/{id}/diagrams` (**null**, not 404, when there is none) |
| `GET` | `/api/v1/admin/projects/{id}/diagrams/{sipoc\|flow\|swimlane}.png?version=` |
| `GET` | `/api/v1/admin/projects/{id}/architecture/{logical\|development\|deployment}.png` — SDD §3.2's three views. No `version`: these are drawn from the *current* design, so there is no earlier revision to ask for |

There is deliberately no admin equivalent of `POST`, `freeze` or `DELETE`: an admin can look at
a process, never redraw or approve one on the user's behalf.

The UI fetches the PNGs as **blobs**, not as `<img src="…">`: auth is a Bearer header
applied by the HTTP interceptor, so a browser-issued image request would arrive
unauthenticated and 401. The wizard and the admin panel both mount one viewer component
(`frontend/src/app/shared/diagram-viewer/`), so the zoom / fit / 100% / pan / full-screen
behaviour cannot drift between them. It also shows SDD §3.2's three architecture views — its
view ids are plain strings for that reason, narrowed back to `DiagramKind` / `ArchitectureKind`
by the guards in `models.ts` — so those get the same controls rather than a lookalike pane.

Both view families are described **once**, in that component's own file:
`DIAGRAM_VIEWS` / `RENDER_SCALE` for the blueprint and `ARCHITECTURE_VIEWS` /
`ARCHITECTURE_RENDER_SCALE` for SDD §3.2. The wizard and the admin console import them rather
than each holding a list of tab labels, so the two screens cannot end up captioning the same
picture differently.

**The admin panel mounts two of these viewers side by side** — Blueprint above, Architecture
below — for the selected user's session, because an admin reviewing a workspace wants the
process *and* the system that runs it. The two are independent: a session can have a generated
plan and no drawn process, or the reverse, so each block decides for itself from
`has_diagrams` / `has_plan` on the session row and neither waits for the other. Both flags are
column reads rather than renders — drawing three architecture PNGs per session inside
`/admin/users` would put nine matplotlib renders per user into a list request.

One consequence worth knowing: the viewer listens on `document` for the bare keys (`f`, `0`,
`1`, `+`, `-`, arrows), so two mounted viewers would both answer `f`. The panel therefore
tracks a `keyboardOwner`, claimed on `mouseenter` / `focusin`, and passes `[live]` to each
viewer — the keys reach whichever figure the pointer or focus is actually on.

Under the figures, a session with a plan also offers **Download PDF** / **Download DOCX** for
that user's `SDD.md` — the same two buttons the owner sees, hitting the admin twin of the same
route. An admin cannot read a `.md` in this panel (it lists names, not contents), so the document
is how they read a design at all; it is also what gets forwarded to somebody without a login.

### Export UI notes

- File tree: every folder starts **collapsed**; click a row to open it, or use
  **Expand all** / **Collapse all** in the tree toolbar
- Default selected file: **`README.md`**
- Zip download uses authenticated API (Bearer token) — works for Cursor / Claude / Windsurf

#### `SDD.md` — the solution design document

Every workspace exports a **Solution Design Document** beside `WORKBREAKDOWN.md`. The two
answer different questions: the work breakdown says *what to build, in what order*; the SDD
says *what the solution is and why it is shaped that way*. It is generated for **every
user** and for an admin through any project they can open — nothing about it is role-gated —
and the CLI produces the identical file (`preview`, `export`, and `wizard` all wait for it).

Fixed numbered outline, checked heading by heading before the document is accepted:

| § | Section | § | Section |
| --- | --- | --- | --- |
| 1 | Project Summary | 3.3 | Development Framework |
| 1.1 | Problem Statement | 3.4 | Setup and Configuration/Migration Requirements |
| 1.2 | Objective | 3.4.1 | Hardware, Software, and Access Requirements |
| 2 | Scope | 3.4.2 | Deployment with Docker |
| 2.1 | In-Scope | 3.5 | Coding Best Practices |
| 2.2 | Functional Requirements | 3.6 | AI Guardrails & Data Security |
| 2.3 | Non-Functional Requirements | 4 | Dependencies |
| 2.4 | Out of Scope | 5 | Assumptions |
| 3 | Solution Definition | 6 | Challenges and Risks |
| 3.1 | Overview | 6.1 | Challenges |
| 3.2 | Architectural Views | 6.2 | Risk |
| 3.2.1 | Logical View | 7 | Acceptance Criteria |
| 3.2.2 | Development View | | |
| 3.2.3 | Deployment View | | |

**The architectural views are pictures, not prose and not code.** Each of §3.2.1–3.2.3
embeds a **real PNG** from `docs/architecture/`, drawn by the same matplotlib renderer as the
SIPOC / process flow / swimlane, and repeats the same content as a table underneath:

| View | Shows | Drawn from |
| --- | --- | --- |
| **Logical** | Layers of responsibility as stacked bands, calls running one way — downward | The model's layers, one band each |
| **Development** | The package layout **this workspace actually exports**, and the import direction between packages | `plan.source_tree` — not the model, so the picture cannot disagree with the files |
| **Deployment** | What runs where at runtime, with protocol, port and payload on each edge | The model's nodes, with a chained fallback |

**Every box carries four lines, not one.** A four-box overview is not a design document, so
each box is drawn as **name / summary / what it does / the concrete thing it owns**:

| View | Line 2 | Line 3 | Line 4 — the checkable one |
| --- | --- | --- | --- |
| **Logical** | The technology — `SQLAlchemy` | What it does — "The only code that queries" | What it **exposes**: `one repository per aggregate`, `POST /batches`, `table batches(status)` |
| **Development** | `9 files · imports 2 · used by 1` | What the package **owns and is not allowed to do** | Its **real filenames** from `plan.source_tree`, packed across up to two lines |
| **Deployment** | The runtime — `uvicorn :8000` | What the process does | Its **state**: `stateless — nothing on local disk`, `stateful — 7-day PITR` |

The fourth line is what makes the document reviewable rather than narrated: a responsibility is
a sentence nobody can be held to, while a route, table, filename or state is something a
reviewer can look for in the scaffold and find missing. Each of those facts also becomes a
column in the table under the figure — *Exposes / contract* in §3.2.1, *Imported by* in §3.2.2,
*State* in §3.2.3.

The **Development View** was deliberately the target of that change. It used to show the
filenames *or* the responsibility and had to choose, which is what made it the thinnest of the
three; it now carries both, and its band subtitle says how deep in the import chain the band
sits rather than only how many packages are in it. Its `imported by` counts are inverted from
the same `depends_on` edges the arrows are drawn from, so the picture and the table cannot
disagree about the blast radius of a change.

Every arrow carries its label, and the legend is worded **per view** — an arrow in the
Development View is an `import`, not a "call", so calling it one made the picture say something
untrue. On the Deployment View the label is two lines: `HTTPS · 443` over `Signed-in user
requests`, because a security group cannot be written from the word "HTTPS" alone.

Both prompts ask for 4–6 layers of 4–7 components, 10–18 labelled flows and 4–7 nodes of 2–4
processes, each with all four fields; the renderer caps at 12 boxes per band and 32 edges, so a
runaway extraction produces a coarse picture rather than an unreadable one. A component
supplied as a plain string still draws — as a one-line box — which is what keeps designs stored
before this existed rendering. The deterministic no-LLM path supplies the fourth line too, so
an export that never reached Bedrock is detailed as well.

**Why not Mermaid.** A fenced ```` ```mermaid ```` block is a diagram in GitHub and in an IDE
preview, and a wall of code everywhere else — this app's own file pane, a pasted excerpt, a
Word document a delivery head sends on. An image is an image in all of them. The tables stay
because a PNG is not searchable, not diffable in a pull request, and not readable by a screen
reader; the picture is how the shape is read, the table is what is reviewed.

Storage mirrors the blueprint's, one folder over — `docs/architecture/logical-view.png`,
`development-view.png`, `deployment-view.png`, written into the export and cached under
`diagrams/<project_id>/architecture/<kind>-<digest>-s3.png`. The digest is a hash of the
views themselves **plus `ARCH_REVISION`**, so a design change draws a new file and prunes the
old one — and so does improving the renderer, which is what stops an existing project being
served last month's picture. There is no version number: unlike the blueprint these are a
drawing of the *current* design, not of an approved revision. The structure behind them is stored on the plan as `architecture_views`,
so the PNGs redraw without another model call; a project generated before this feature derives
them from its plan instead, and its Development View is still the real source tree.

Every edge is filtered before it is drawn: one whose `from`/`to` doesn't match a declared box
or band is dropped rather than pointed at a phantom, and if that empties a view a
deterministic chain is used, so a diagram is never blank. Long connectors are routed through
empty channels between the bands rather than across an intervening box, which is what keeps an
arrowhead attributable to the box it actually belongs to. A connector between two boxes in the
*same* band goes under it rather than through the gap between them, because a caption centred
on a stub arrow is drawn straight across both boxes — and when that band is the last one, the
picture grows a lane so the caption doesn't land on the legend.

Everything a box says is also a table row, so a reviewer working from the document alone loses
nothing to the PNG:

| Under | Table | Columns |
| --- | --- | --- |
| §3.2.1 | **Components in detail** | Component · Layer · Built with · Responsibility |
| §3.2.2 | Package table | Package / path · Files · **Key files** · Imports · Responsibility |
| §3.2.3 | Node table, then **Processes in detail** | Process · Runs on · Built with · What it does |
| §3.2.3 | **Network hops** | From · To · Protocol · **Port** · **Carries** — read as the security-group list, so an unknown port says `confirm` rather than sitting empty |

| Endpoint | Returns |
| --- | --- |
| `GET /api/v1/projects/{id}/plan/architecture/{kind}.png` | One view — `logical`, `development` or `deployment`. Fetched as a **blob**, not an `<img src>`: auth is a Bearer header, so a bare URL 401s |
| `GET /api/v1/projects/{id}/plan/solution-design.{pdf\|docx}` | The whole document as a download (below). An unknown extension is a **404** |
| `GET /api/v1/admin/projects/{id}/solution-design.{pdf\|docx}` | The same document for a session the admin does not own — read-only |

In the UI the three `docs/architecture/*.png` rows open in the file pane with Copy / Edit /
Save hidden, exactly as the blueprint PNGs do — and in the **same viewer**, so they get
**−/+ zoom, Fit, 100%, drag-to-pan, Full screen, Open PNG and Download PNG**, plus
<kbd>0</kbd> / <kbd>1</kbd> / <kbd>f</kbd> and <kbd>←</kbd> <kbd>→</kbd> to move between the
three views (which moves the tree selection with it, so the highlighted row is always the
picture on screen). They used to be a plain `<img>` capped at 62vh, which on a 2526 × 3078
render meant a 540px-wide column of unreadable boxes with empty margins either side.

The same pictures appear inline in SDD.md's own markdown preview, **fitted to the full width of
the pane** — they are drawn at 3×, so at natural size the first figure ran off the right edge
and put a horizontal scrollbar under the whole document. Clicking a figure opens that view in
the viewer above, which is where reading it at 100% belongs; the cursor and a hover title say
so.

**Download it as a PDF or a Word document.** Select `SDD.md` anywhere it appears — the Review
step's **Files** pane, the Export step's file browser, or the **Diagrams** tab under the
architecture filename chips — and two buttons appear beside Copy / Edit / Save: **Download PDF**
and **Download DOCX**. A super admin gets the same pair under each session's figures in the
admin panel, and the terminal has both as well:

```bash
agentcraft plan document --format both -o ./out     # your own session
agentcraft admin document <session-id> --format pdf # any user's, read-only
```

| | What it is |
| --- | --- |
| **Same document, two writers** | One markdown parse feeds both writers (`app/services/export/documents.py`), so the PDF and the DOCX cannot disagree — same headings, same tables, same figures, same order. `reportlab` lays the PDF out, `python-docx` the DOCX |
| **Same as the screen** | The writers implement the *same* markdown subset as the app's own preview — GFM tables, headings past `###`, links, code spans and fences, `---` rules, `**bold**` / `_italic_` — so the document reads on paper the way it reads in the pane. Adding a mark means editing both `formatMarkdown` and `documents.py`, which is why `npm run verify:markdown` asserts each one |
| **Figures included** | §3.2's three architecture PNGs are **embedded**, not linked, with their `*Figure 3.2.x*` captions underneath. The picture set is chosen from the markdown itself, so a document is never charged for a render it does not show, and a blueprint figure would be embedded on the same rule |
| **The admin's copy is the user's copy** | Both routes call one service method. The two files are not byte-identical — a PDF carries `/CreationDate`, a DOCX a modification time per zip entry — but their text and their embedded images are, which is the claim worth making: an admin must be reading what the user has |
| **No model call** | A session whose design has never been generated is backfilled deterministically, exactly as the PNG routes do it. A Bedrock call behind a download button is a reader watching a spinner |
| **Not cached** | The document is laid out per request and streamed as an `attachment` with a project-named filename (`my-project-SDD.pdf`). Nothing to prune, and an edited `SDD.md` is in the next download without a refresh step |

The buttons disable together while one is in flight: the layout happens on the request, so firing
the second on top of the first queues a second render for no gain. A JSON error body served with
a 200 is caught and shown as a message rather than saved as a `.pdf` the reader's viewer then
refuses to open with nothing said about why.

**Why the model returns JSON, not markdown.** The section numbering is a contract the
validator checks; a model asked for markdown renumbers, merges 3.4.1 into 3.4, or stops
mid-table. The document is rendered from JSON **by code**, so the outline cannot drift.
Generation runs in three tiers — three parallel section-group calls, then one single call,
then salvage of whichever groups validated, then the deterministic renderer — because a
document with 26 required headings shouldn't be thrown away over one short list.

`is_solution_design_complete` requires all 26 headings present **and in order**, ≥5,000
characters, **all three figure references** (`](docs/architecture/…-view.png)` — a document
that lost a picture still reads as prose, so nothing else would notice), a balanced fence
count, an `| ID |` table, and a last line that isn't a bare heading.
`backend/scripts/verify_solution_design.py` exercises all of it offline — including that no
```` ```mermaid ```` fence survives anywhere, that each figure carries a `*Figure 3.2.x*` caption,
and that the three PNGs really render — no Bedrock, no database, no pytest.

`backend/scripts/_admin_arch_roundtrip.py` covers the half a renderer check cannot: it drives
the real ASGI app in-process with `TestClient` against a throwaway SQLite file, walks the whole
flow as two different users (super admin, plus an owner it signs up and approves), and then
asserts that the admin's read-only route serves three real PNGs **byte-for-byte identical** to
what the owner's own route serves, that the owner is `403` and an anonymous caller `401` on the
admin route, that `/admin/users` reports `has_plan`, and that the export preview carries
`docs/architecture/` together with the README section describing it. It then downloads the
document in **both** formats from **both** sides and asserts the admin's copy is the same
document as the owner's — compared by extracted text and image digests rather than bytes, for the
timestamp reason above — that the figures really are embedded in each, that the download routes
are gated exactly as the PNG routes are, that an unknown extension is a 404, and that the CLI's
own helper writes the same two files (imported from `cli/agentcraft/main.py` and driven against
the in-process app, not re-implemented, because a copy of the helper could not show that the
terminal and the browser agree). Also offline: the interview finishes in one POST because a
structured expanded brief as the `goal` answer takes the branch that prefills the rest without
calling Bedrock. The admin credentials come from settings and are never printed.

Two more instruments sit beside it, both offline and both writing only into gitignored paths.
`backend/scripts/_document_export_sample.py` lays a realistic design out as a PDF and a DOCX into
`backend/.scratch/` — there is no headless browser on these boxes, so rasterising the PDF and
*looking* at it is the only way to catch a caption cut in half or a table breaking mid-word.
`backend/scripts/_truncation_audit.py` draws all six views and reports every way a sentence can be
lost: shortened by the renderer (informational — a long description bounded on a word boundary is
the design working, a **label** or a `·`-joined list losing items is a defect), shortened before
the renderer saw it, or covered by a label's opaque plate. It exits non-zero on the last two, and
on stored prose that ends mid-sentence. It leaves a `backend/_truncation.db` behind — delete it
(`*.db` is gitignored, so a forgotten one is untracked rather than committed).

Edit it in the Export step and **Save**: the body persists as a `file_override`, which
`render_files` applies last, so a hand-edited design document survives every reopen and
refresh. Unlike `WORKBREAKDOWN.md` there is no `refresh_*` counterpart — this document
never described the IDE rules folder, so a stored body has nothing stale to correct.

#### Generated root files

Two files live at the repo root beside `main.py` and are **generated from the plan** on
every render — they are not entries in `source_tree`:

| File | Platforms | Contents |
| --- | --- | --- |
| `.gitignore` | all | Real ignore rules for the detected stack |
| `CLAUDE.md` | Claude Code only | Short project memory Claude Code auto-loads |

Because they are rendered rather than stored, **every existing project picks them up on
its next preview / refresh / export** — no migration, no regeneration. Verified against
all 19 saved projects. Edit either one in the Export step and **Save**; the edit persists
as a `file_override` and survives refresh.

**Why `CLAUDE.md` matters.** Claude Code reads it from the project root at the start of
every session, so its cost is paid on *every* prompt. That is exactly why it is kept
short — under 60 lines — and points at `README.md` / `WORKBREAKDOWN.md` instead of
repeating them. A long memory file crowds out the actual task and gets skimmed. What
earns a place in it is only what Claude can't infer from the code:

- **Start here** — read `WORKBREAKDOWN.md`, one phase at a time, with a *pointer* (not an
  `@` import) to `SDD.md` for why the solution is shaped the way it is. Importing a long
  design document would be paid for on every prompt; it is there to open when a decision
  needs its reasoning
- **Layout** — which directory owns what, listing *only* directories this plan has
  (a bullet for a `frontend/` that was never scaffolded sends Claude hunting for nothing)
- **Rules** — `@` imports for the always-on rules, plus a list of the glob-scoped ones
  (see [Rules](#rules--every-ide-always-project-specific)). The layer conventions live in the rule
  files, not restated here, so there is exactly one copy to keep correct
- **Commands** — install / run / test, matched to the stack
- **Agents and skills** — this project's actual names, so Claude delegates instead of
  doing specialist work inline

It goes at the **root**, not inside `.claude/` — Claude Code only auto-loads project
memory from the root. Cursor and Windsurf don't get one: Cursor reads `.cursor/rules`
and Windsurf reads `AGENTS.md`, so a `CLAUDE.md` there would be an unread file.

**Why `.gitignore` is stack-aware.** Blocks are emitted only when the scaffold paths call
for them, so a Python-only API doesn't ship `node_modules/` noise:

| Detected | Adds |
| --- | --- |
| `.py` / `requirements.txt` | `__pycache__/`, `.venv/`, `.pytest_cache/`, coverage |
| `.ts` / `.tsx` / `package.json` | `node_modules/`, logs, `*.tsbuildinfo` |
| `frontend/app/*.tsx` (Next) | `.next/`, `out/` |
| `*.component.ts` (Angular) | `.angular/`, `dist/` |
| SQLite paths | `*.db`, `*.sqlite3` |
| Docker / migrations | override compose files, `.pyc` in versions |

Always included: `.env` (with `!.env.example` kept), keys/pems, editor and OS junk, logs.

Detection reads the **scaffold paths, not the brief**, so it behaves identically for a
plan generated today and one generated months ago. Angular is checked before Next since
both put `.ts` under `frontend/`.

The AI workspace — `.claude/` `.cursor/` `.windsurf/` `AGENTS.md` `CLAUDE.md`
`WORKBREAKDOWN.md` `SDD.md` — is deliberately **not ignored**. Those files are the point of the
export; commit them so the whole team gets the same agents, skills, and rules. Only
machine-local IDE state (`settings.local.json`) is ignored.

Both files count toward **Workspace files** and never toward **Source files** — see below.

#### Rules — every IDE, always project-specific

Every IDE exports rules. Only the folder and dialect differ:

| IDE | Folder | Dialect | Apply mode lives in |
| --- | --- | --- | --- |
| Claude Code | `.claude/rules/` | plain `.md`, **no frontmatter** | `CLAUDE.md` `@` import + an `Applies to:` line in the body |
| Cursor | `.cursor/rules/` | `.mdc` | `alwaysApply: true` or a `globs:` list in frontmatter |
| Windsurf | `.windsurf/rules/` | `.md` | a `trigger:` field (`always_on` / `glob` / `model_decision`) |

```
my-project/
├── CLAUDE.md                    # Claude Code only
├── .claude/                     # or .cursor/ · .windsurf/
│   ├── agents/…
│   ├── skills/…
│   └── rules/
│       ├── architecture.md
│       ├── secure-coding-vapt.md
│       ├── workflow.md
│       ├── frontend.md          # only when a frontend was scaffolded
│       └── testing.md
```

**Claude Code has no frontmatter dialect.** Cursor's `.mdc` has an `alwaysApply` / `globs`
block and Windsurf has `trigger`; a YAML header in a Claude rule would just render as
literal text at the top of the file. So there the apply mode is expressed the two ways
Claude actually reads:

| Apply mode | How it is encoded |
| --- | --- |
| always | `CLAUDE.md` imports it with `@.claude/rules/<name>.md`, so it is in context every session, and the body opens with `**Applies to: always.**` |
| glob-scoped | body opens with `**Applies to: \`backend/tests/**/*.py\`.**`, and `CLAUDE.md` *lists* the path with its globs instead of importing it |

Only always-on rules are imported. Importing all of them would put every rule in context
on every prompt — the exact cost `CLAUDE.md` is kept short to avoid.

**Project-specific, not boilerplate.** The LLM is asked for rules, but what comes back
varies — sometimes rich, sometimes two generic bullets, sometimes nothing. So rules are
also *derived* from the plan's own scaffold, agents, and skills, giving every IDE the same
floor and making two projects come out different:

| Rule | What makes it this project's |
| --- | --- |
| `architecture` | names the real domain slices read off `backend/services/*_service.py` — the IDE sees `payments`, `ledger`, not "your domain"; adds a Redis line only when `backend/core/redis.py` exists |
| `secure-coding-vapt` | VAPT gate (SQLi, XSS, log injection, IDOR, SSRF, secrets); points at `backend/core/security.py` when the scaffold has it |
| `workflow` | delegates to this project's actual agent and skill names, in *this* IDE's folders |
| `frontend` | omitted entirely when there is no frontend; otherwise Jinja / Angular / Next guidance with matching globs |
| `testing` | scoped to `backend/tests/**/*.py`, plus `frontend/tests/**` when present |

**Existing projects are fixed too.** Rules are rendered from the plan on every
preview / refresh / export — the same mechanism as `.gitignore` and `CLAUDE.md` — and the
resolved set is persisted on read so the Review step's **Rules** tab, the Studio sessions
`R` count, and the exported folder can never disagree. A project that already carries rules
keeps them and only gains missing *always-on* ones, so a rule you deliberately deleted does
not come back. Each project's `README.md` and `WORKBREAKDOWN.md` are updated in place: the
stale "Claude Code exports agents and skills only" line in `WORKBREAKDOWN.md` is replaced
with the real rule list, leaving every hand-edited phase untouched.

#### The two file counts

Two different quantities, one name each — they are **supposed** to differ:

| Name | What it counts | Where it appears |
| --- | --- | --- |
| **Source files** | The application scaffold only (`source_tree`, minus anything that is really an IDE artifact) | Review hint, the **Files (N)** tab badge, the exported README's `Files:` line, CLI `plan` summary and `preview` footer |
| **Workspace files** | **Every** path in the export | Export step heading, tree toolbar, the **Refresh file tree** message, the exported README, CLI `preview` footer |

```
workspace = source
          + IDE folder (agents / skills / rules / mcp)
          + README.md + WORKBREAKDOWN.md + SDD.md + .gitignore
          + CLAUDE.md              (Claude Code only)
          + docs/diagrams/*.png    (3, if a blueprint was approved)
          + docs/architecture/*.png (3, the views SDD.md §3.2 embeds)
```

So a project with 139 source files and 165 workspace files has ~26 files in
`.cursor/` (or `.claude/` / `.windsurf/`) plus the generated root files. Nothing is out
of sync — the gap is the IDE artifacts.

The workspace total in the exported README is a placeholder
(`{{AGENTCRAFT_WORKSPACE_FILE_COUNT}}`) that `render_files` substitutes **after** every
path exists, including per-file overrides. The README describes the file set it is
written into, so counting by rendering rather than by arithmetic is what makes drift
impossible. `scaffold_file_count()` in the backend renderer is the one definition of
"source files"; `isIdeOrReadmePath` (Angular) and `_is_ide_or_readme` (CLI) mirror it —
all three list `.gitignore` and `CLAUDE.md` as generated, so neither moves the badge.

#### What **Refresh file tree** does

It **re-renders** the workspace from the saved plan — it does not regenerate anything:

- picks up plan edits you saved (agents, skills, rules, scaffold files)
- picks up **`WORKBREAKDOWN.md`** and **`SDD.md`** once background generation finishes
- backfills `source_tree` on older projects, so their scaffold becomes editable
- **does not** call Bedrock or re-deduce agents / skills / rules

Folders you had open stay open. The CLI equivalent is simply re-running
`agentcraft preview`.

#### Why expanding a folder is instant

A 137-path workspace only ever renders the rows you can actually see — 9 with
everything collapsed, 178 fully expanded. Three things make the click feel immediate:

| Change | Why it mattered |
| --- | --- |
| Tree renders one flat `@for` over visible rows | It used to re-enter a recursive `<ng-template>` per folder depth, so opening one folder built a whole nested stack of views and every change-detection pass walked all of them |
| `toggleFolder` calls `detectChanges()` | Change detection is coalesced app-wide, so flipping `expanded` alone defers the repaint to the next coalesced pass — a frame, not seconds, but visible on a direct click |
| Open folders are remembered by path | The tree is rebuilt on every file reload — including the 4-second poll while `WORKBREAKDOWN.md` generates. A folder opened a moment earlier used to **collapse itself**, which looked exactly like the expand having failed |

The rows are also keyed by full path, so splicing children in leaves every other row's
DOM untouched, and the press state is colour-only — the global button `scale(0.97)`
used to shrink the row just as the click landed.

#### Why **Edit** is instant

There are six `Edit` buttons in Review/Export — the plan **Summary**, the
**agent / skill / rule** card bodies, and the file pane on both the **Files** tab and the
**Export** step. All six now flip on the frame you click them.

The slow one was the file pane, and the cost was **rebuilding the preview, not the click**.
The rendered markdown used to sit in the `@else` of an `@if`, so every toggle destroyed and
re-created it — for a 430-line `WORKBREAKDOWN.md` that is ~48 KB of HTML rebuilt
synchronously before the browser can paint.

| Change | Why it mattered |
| --- | --- |
| Editor and preview both stay mounted, taking turns being `[hidden]` | Toggling `Edit` became a style flip instead of re-parsing the whole file into DOM. `.file-body[hidden] { display: none; }` is stated explicitly, because a class setting its own `display` would beat the UA sheet on specificity |
| The preview renders a **snapshot** (`filePreviewBody`), not the textarea's live model | Now that the preview is always in the DOM, binding it to the model would re-parse the entire file on **every keystroke**. The snapshot advances when you return to Preview |
| All six buttons call a handler that ends in `detectChanges()` | They used to mutate the flag inline in the template, which deferred the repaint to the next coalesced pass |
| The markdown cache is keyed on the **whole body** | The old key was length + first 48 characters, so a same-length edit — fixing a typo, swapping a word — hashed identically and Preview redisplayed the **pre-edit** text |
| The 4-second document poll no longer overwrites an open editor | It used to reset the file pane and replace the draft plan wholesale, discarding whatever you had typed. It now adopts only the six `work_breakdown*` / `solution_design*` fields while anything is in flight — skipping outright would let the next **Save** push a stale document over the freshly generated one |
| `hasSavedPlan` is a boolean, not a serialised plan | It held a full `JSON.stringify` of every agent prompt, skill body and scaffold file (4–7 ms), re-run on every poll tick, though every reader only tested it for truthiness |

Measured in a real browser on this repo's projects: click → correct pane visible is
**34–265 ms** for a 430-line file (was a full re-parse), card and Summary toggles are
**37–261 ms**, and ten keystrokes in the editor leave the preview untouched.

#### What the markdown preview renders

The preview is a **hand-rolled renderer** (`formatMarkdown`), not a library — which is why it
had gaps, and why the gaps are now covered by a check script rather than by hope. `SDD.md`
found all of them at once: its table of contents reached the pane as rows of literal
`| § | Section |`, `#### 3.2.1 Logical View` as literal hashes, and its links as raw
`[text](url)`.

| Renders | Note |
| --- | --- |
| **GFM pipe tables** | A separator row is **required**, so a paragraph that merely contains a `\|` stays a paragraph. `:--` / `:-:` / `--:` become `text-align`, a backslash-escaped pipe stays inside its cell instead of shifting the columns, and a row longer than the header is truncated to the header's column count — as GFM does — rather than growing a ragged table |
| **Headings `#` to `######`** | It stopped at `###`; SDD.md numbers §3.2.1 at four and the TOC anchors point at them |
| **Links** | Through `safeHref`, an **allowlist**: `https?://host`, `mailto:`, `#anchor`, and a relative workspace path. `javascript:`, mixed-case `JaVaScRiPt:`, `data:`, `vbscript:` and protocol-relative `//host/x` are all refused — the link text is kept, the `href` is not |
| **Images** | Only the three `docs/architecture/` views, because they are the only images any generated document embeds. Anything else renders as a **named placeholder**, not a broken-image icon. A figure is fitted to the **full pane width** (`width: 100%`, not `max-width` alone — these are 3× renders, so bounding them was not enough to stop the pane scrolling sideways) and **opens in the diagram viewer on click** |
| **Code spans** | Lifted out **before** the link and emphasis passes, so a URL or a `**glob**` inside backticks stays literal |
| **Thematic breaks** | `---`, `***`, `___` — three or more marks, and the marks may be spaced out, so `* * *` is a rule. Tested **before** the bullet rule, or that spaced form parses as a one-item list of nothing. SDD.md separates its sections with them, and without this each one reached the pane as three literal dashes |
| **`_italic_`** | Guarded: the underscores must stand alone. `batch_service.py`, `snake_case_name` and `__dunder__` are everywhere in these documents and none of them is emphasis |

Every row above is also implemented in `backend/app/services/export/documents.py`, which is what
writes the PDF and the DOCX — the preview and the download are three renderers of one subset, so
a new mark is an edit in both files and an assertion in the check script. That is exactly what
caught `* * *` being read as a list in *both* implementations at once.

Two details worth keeping if this is ever touched:

- This HTML goes through `bypassSecurityTrustHtml`, so `safeHref` **is** the sanitiser. There
  is no second gate behind it.
- `renderMarkdown` caches on the source text, so anything that changes the *render* without
  changing the *text* — an architecture blob arriving a moment after the file opened — must
  call `clearMarkdownCache()`, or the pane keeps showing placeholders.

`npm run verify:markdown` (`frontend/scripts/verify-markdown-preview.mjs`) checks all of it
with no browser and no test runner: it lifts the real method bodies out of the component
between two markers — so a rename **fails the script** rather than passing against a stale
copy — compiles them with the project's own `tsc`, and runs ~40 assertions, the last group
against an actual `SDD.md` rendered by the backend (≥8 real `<table>`s, exactly three `<img>`s,
no leftover pipes, hashes or placeholder characters).

---

## 4. CLI — install

```bash
cd cli
python -m venv .venv
```

**Windows PowerShell:**

```powershell
.\.venv\Scripts\Activate.ps1
pip install -e .
$env:PYTHONIOENCODING = "utf-8"
agentcraft --help
```

**Windows `cmd.exe`:**

```bat
.venv\Scripts\activate.bat
pip install -e .
set PYTHONIOENCODING=utf-8
agentcraft --help
```

`PYTHONIOENCODING=utf-8` keeps the classic console from choking on the box-drawing and
arrow characters Rich prints.

**macOS / Linux:**

```bash
source .venv/bin/activate
pip install -e .
agentcraft --help
```

No API URL to set: the CLI reads `API_PORT` from the repo-root `.env` and targets
`http://127.0.0.1:$API_PORT`. The same command works on a server — see
[Running the CLI on EC2](#running-the-cli-on-ec2) if it cannot connect.

Set `AGENTCRAFT_API_URL` only to reach an API somewhere other than loopback:

```bash
export AGENTCRAFT_API_URL=http://44.194.125.199:8555   # CLI on your laptop, API on EC2
```

State files under `~/.agentcraft/`:

| File | Purpose |
| --- | --- |
| `token` | JWT after **approved** login |
| `current` | Active project id |

---

## 5. Auth & approval (CLI = same rules as UI)

### Super admin login + manage users

```bash
agentcraft health
agentcraft auth login --email admin@ac.com --password "1681149@sPk"

# Same info as Admin UI — tabular overview + per-user session tables
agentcraft admin users
agentcraft admin users --status pending
agentcraft admin users --status deleted     # the archive, same as the UI's Deleted tab
agentcraft admin users --email phani@ac.com

agentcraft admin approve <user-uuid>
agentcraft admin reject <user-uuid>

# Revoke access; workspaces are held so this can be undone (prompts first; -y for scripts)
agentcraft admin delete <user-uuid>
agentcraft admin delete <user-uuid> --yes

# Undo a delete — original password, previous status, workspaces reattached
agentcraft admin restore <user-uuid>

# Erase a deleted account for good: archive + held workspaces (not reversible)
agentcraft admin purge <user-uuid> --yes
```

`admin delete` prints the target’s name, status, and session count before asking, refuses
the super admin account, and ends with the exact `admin restore` command to undo it.
`admin users` lists deleted accounts as `deleted (was approved)` with their workspaces
marked *held*, followed by ready-to-paste `restore` / `purge` lines. `purge` refuses an
account that is not deleted, and `delete` refuses one that already is.

Tables show:

1. **Users overview** — name, email, role, status, session count  
2. **Sessions for each user** — session title, state, platform, counts (`#A/#S/#R`), then **Agents / Skills / Rules** names (one per line in the cell)

Super admin can also create **their own** projects (same as a normal user):

```bash
agentcraft init -n "Admin Demo Workspace"
agentcraft path docs
agentcraft docs add .\brief.md -s "Your problem statement here."
agentcraft platform set cursor
agentcraft generate --demo
agentcraft export --zip .\admin-demo.zip
agentcraft sessions
# or interactive:
agentcraft wizard
```

Regular users get the same session table shape:

```bash
agentcraft sessions
```

### New user signup (password + confirm)

Interactive:

```bash
agentcraft auth signup
# prompts: email, name, password, confirm password
```

Non-interactive:

```bash
agentcraft auth signup \
  --email you@example.com \
  --name "You" \
  --password secret12 \
  --confirm-password secret12
```

- Signup creates a **pending** account — **no JWT**
- Login before approval → error: waiting for admin approval
- Login after the admin **deleted** the account → “Your account was deleted by the admin”
  (the stored token is removed). The admin can undo it with `admin restore`, which keeps
  this same password; signing up again works too, but starts a fresh `pending` account
- After admin approves:

```bash
agentcraft auth login --email you@example.com --password secret12
agentcraft auth whoami
```

---

## 6. CLI — offline demo (`--demo`, no Bedrock)

Test escape hatch only — the UI always uses live Bedrock. With API running and an **approved** user logged in:

```powershell
$env:PYTHONIOENCODING = "utf-8"

agentcraft init -n "CLI FleetPulse Demo"
agentcraft path docs
# Create a short brief.md then:
agentcraft docs add .\brief.md -s "Build a fleet telematics dashboard with alerts, maps, and RBAC."
# Optional blueprint step — three PNGs land in .lueprint, then approve them
agentcraft diagrams generate --demo
agentcraft diagrams show
agentcraft diagrams refine "operations also acknowledges every critical alert"
# A change to one picture only — the process, the other two views and the agents stay put
agentcraft diagrams refine "group the SIPOC inputs by source system" --scope sipoc
# The redraw saves as *-v2.png; write v1's three alongside it (the UI's history click)
agentcraft diagrams save --version 1
agentcraft diagrams freeze
agentcraft platform list
agentcraft platform set cursor
agentcraft generate --demo
agentcraft plan show
agentcraft status
# Browse the workspace tree — mirrors the UI's Export step
agentcraft preview -l -d 1        # top level only, deeper folders show "(N files)"
agentcraft preview -l             # everything, like the UI's "Expand all"
# Footer prints both counts: "Source files: 108 · Workspace files: 137"
# …and a `Rules: .cursor/rules/ (N — .mdc with alwaysApply/globs)` line
agentcraft preview -f .cursor/rules/architecture.mdc   # one project-specific rule
agentcraft export --zip .\fleetpulse-cursor.zip
# Zip includes .cursor/* AND modular app scaffold (main.py, app/**/__init__.py, …)
# README.md lists Artifact counts (both file counts) + Project structure + File purposes
agentcraft sessions --state exported --platform cursor
```

**Claude Code:**

```bash
agentcraft init -n "CLI Claude Demo"
agentcraft path docs
agentcraft docs add .\brief.md -s "AI minutes of meeting with action items."
agentcraft platform set claude_code
agentcraft generate --demo
agentcraft preview -f CLAUDE.md                      # project memory, loaded each session
agentcraft preview -f .claude/rules/architecture.md  # one project-specific rule
agentcraft export --zip .\mom-claude.zip
# preview prints a `Rules: .claude/rules/ (5 — plain .md, imported by CLAUDE.md)` line
```

**Windsurf:**

```bash
agentcraft init -n "CLI Windsurf Demo"
agentcraft path docs
agentcraft docs add .\brief.md -s "Payments API with ledger and webhooks."
agentcraft platform set windsurf
agentcraft generate --demo
agentcraft export --zip .\payments-windsurf.zip
```

Directory export:

```bash
agentcraft export --out .\out-workspace
```

Live Bedrock (default — same as UI; omit `--demo`):

```bash
agentcraft generate
```

Interactive wizard (auth → path → IDE → **live** generate → zip):

```bash
agentcraft wizard
```

---

## 7. CLI step-by-step state machine

| Step | Command | Result |
| --- | --- | --- |
| A | Backend running | `:8555` (`API_PORT`) |
| B | `pip install -e .` in `cli/` | `agentcraft` available |
| C | `auth signup` → admin approve → `auth login` | JWT in `~/.agentcraft/token` |
| D | `init -n "Name"` | `CREATED` |
| E | `path docs` or `path interview` | awaiting docs/interview |
| F | `docs statement "..."` or `interview` | `CONTEXT_READY` |
| F2 | `diagrams generate` → `diagrams refine "..."` → `diagrams freeze` | **optional**; blueprint approved (PNGs written to `./blueprint`) |
| G | `platform set cursor\|claude_code\|windsurf` | `PLATFORM_SELECTED` |
| H | `generate [--demo]` | `READY_FOR_REVIEW` |
| I | `plan show` / `plan edit --json file` | optional |
| J | `export --zip` or `--out` | `EXPORTED` + detailed README **and** Bedrock `WORKBREAKDOWN.md` + `SDD.md` in zip |

---

## 8. CLI command reference

| Command | Purpose |
| --- | --- |
| `agentcraft health` | API health (no auth) |
| `agentcraft auth signup` | Request access (password + confirm; pending) |
| `agentcraft auth login \| logout \| whoami` | JWT session |
| `agentcraft admin users [--status pending\|deleted]` | List users (incl. deleted archive) + per-session **Pipeline** (Documents / Interview / not chosen — `?` means deduced), **Blueprint** (version, approved or draft, step / actor counts) and artifact names |
| `agentcraft admin diagrams <session-id> [--out DIR] [--version N] [--no-save]` | Read **any** user's blueprint and write its three PNGs, **plus SDD §3.2's three architectural views** into `<out>/architecture/` — the CLI half of the admin panel's two viewers. The two are independent, so a session with a plan and no drawn process still writes the architecture (and prints a *No blueprint* line), and the reverse. Read-only: there is deliberately no admin `generate` / `refine` / `freeze` / `discard` |
| `agentcraft admin document <session-id> [--format pdf\|docx\|both] [--out DIR]` | Download **any** user's `SDD.md` as a PDF and/or a Word document — the terminal half of the admin panel's download buttons. Read-only: the document is laid out from what the session already holds and is never regenerated on the owner's behalf |
| `agentcraft admin approve \| reject <user-id>` | Approval actions |
| `agentcraft admin delete <user-id> [--yes]` | Revoke access, keep workspaces for restore (not the super admin) |
| `agentcraft admin restore <user-id>` | Undo a delete — original password, status, workspaces |
| `agentcraft admin purge <user-id> [--yes]` | Erase a deleted account + held workspaces for good |
| `agentcraft sessions [--state …] [--platform …]` | Your workspaces (filters match UI KPIs) |
| `agentcraft delete-session <id>` | Delete a workspace |
| `agentcraft wizard` | Full interactive flow (live Bedrock; no demo prompt) |
| `agentcraft init -n "Name"` | Create project |
| `agentcraft status [-p ID]` | State / blockers |
| `agentcraft path docs\|interview` | Choose path |
| `agentcraft rollback` | One-step back **before** generate only |
| `agentcraft docs statement "..."` | (use `docs add` — statement + files required) |
| `agentcraft interview expand "..."` | Expand short seed (same as UI interview Expand) |
| `agentcraft docs add file.pdf img.png -s "..."` | Path A: statement + multi-file (png/jpeg/pdf/docx/md/txt/csv/tsv/xlsx/xls) |
| `agentcraft docs list [-p ID]` | Documents saved with a project (name / type / size / extracted chars) |
| `agentcraft interview` | Path B Q&A |
| `agentcraft interview answer --id Q --text "..."` | Answer one question without the prompt loop (for scripts) |
| `agentcraft diagrams generate [--demo] [--out DIR]` | Draw SIPOC + process flow + swimlane; writes three PNGs (default `./blueprint`) |
| `agentcraft diagrams show [--json]` | Print the stored blueprint in full — a legend saying **what each of the three views is for**, then every step with its actor, systems and description (nothing ellipsised, as in the UI's step list), SIPOC columns, lanes, **stage names**, warnings, and the change history marking which versions can still be drawn |
| `agentcraft diagrams refine "..." [--scope all\|sipoc\|flow\|swimlane]` | One plain-language change (**reopens** the blueprint, whichever scope). `--scope all` (default, alias `--only`) revises the **process** and redraws all three views, so generation follows it; `--scope sipoc\|flow\|swimlane` re-lays out that one view — grouping, ordering, wording, level of detail — and leaves the process, the other two views and the generated agents alone — including its wording, so *"re-name the suppliers and customers in title case"* under `--scope sipoc` is applied, not answered. Prints which it is doing before the call, since a process revision is the slower one. If the follow-up changed nothing, prints the server's amber *Nothing changed* advice and exits **1** without recording a version — the saved PNGs are still current |
| `agentcraft diagrams freeze \| unfreeze` | Approve / reopen. Generation is blocked while a blueprint is unapproved |
| `agentcraft diagrams save [--out DIR] [--version N]` | Re-download the three PNGs as `sipoc-v2.png` / `process-flow-v2.png` / `swimlane-v2.png` — **the same names the zip's `docs/diagrams/` uses**, so a file here and a file there are comparable by name and saving after a revision never overwrites a reviewed set. Each line reports its pixel size, so a supersampled render is visibly not a stale file. `--version` writes an earlier version from the change history (the UI's "view images" on a history entry); a version that has aged out lists the ones still available. Ends with the same what-each-view-is-for legend, so three filenames are not left unexplained |
| `agentcraft diagrams discard [--yes]` | Drop the blueprint — project returns to generating from the brief alone |
| `agentcraft platform list\|set …` | IDE |
| `agentcraft generate [--demo]` | Build agents/skills/rules; `--demo` = offline tests only |
| `agentcraft plan show\|tree\|edit --json …` | Review JSON / structure / replace |
| `agentcraft plan document [--format pdf\|docx\|both] [--out DIR]` | Write `SDD.md` as a PDF and/or a Word document, §3.2's three architectural views embedded. Both come off **one** parse of the markdown, so they cannot disagree with each other or with the screen; the filename comes from the response header, so it matches what the browser downloads. Nothing is generated — a session whose design has never been written gets the deterministic document rather than a Bedrock call behind a download |
| `agentcraft preview [-f FILE]` | Export file tree / README (same as UI) |
| `agentcraft preview -l -d 1` | Tree collapsed to top level — deeper folders show `(N files)`, like the UI's collapsed rows |
| `agentcraft preview -f CLAUDE.md` | Claude Code project memory (Claude Code projects only) |
| `agentcraft preview -f .gitignore` | Generated stack-specific ignore rules |
| `agentcraft preview -f <rules-dir>/<name>` | One project-specific rule (`.claude/rules/*.md`, `.cursor/rules/*.mdc`, `.windsurf/rules/*.md`) |
| `agentcraft export --zip\|--out` | Download artifacts |
| `agentcraft config show` | LLM settings (login required) |
| `agentcraft config set-model` | Repoint generation at another Bedrock model — **super admin only**, and process-wide |

---

## 9. Feature parity (UI ↔ CLI)

| Feature | UI | CLI |
| --- | --- | --- |
| Signup needs admin approval | Yes | Yes |
| Password + confirm password | Yes | Yes (`--confirm-password` or prompt) |
| Reveal what you typed | Eye toggle in every password field (login + both signup fields, each independent) | n/a — the terminal never echoes the prompt |
| Super admin approve/reject | `/admin` | `agentcraft admin …` |
| Super admin **delete user** (revokes access) | **Delete user** + confirm modal | `admin delete [--yes]` |
| See deleted accounts | **Deleted** KPI + filter chip | `admin users --status deleted` |
| **Restore** a deleted user (+ workspaces) | **Restore user** | `admin restore` |
| **Erase** a deleted user for good | **Erase permanently** + confirm modal | `admin purge [--yes]` |
| Deleted account is told why | Login banner + forced logout | Same message; stale token cleared |
| Spinner while attaching documents | Dropzone spinner + disabled buttons | n/a (local paths read directly) |
| Remove an attached document | Chip **×** / **Remove all** | Re-run `docs add` with the files you want |
| See session agent/skill/rule **names** | Sessions + Admin expand | `sessions` / `admin users` |
| Session filters (exported / ready / in progress / IDE) | KPI chips | `sessions --state … --platform …` |
| Super admin creates **own** projects | **New workspace** / **My sessions** | `init` / `wizard` / `sessions` |
| Other users’ workspaces | Names, pipeline and diagrams in `/admin` — read-only | `admin users` + `admin diagrams <session-id>` |
| Which context pipeline a session followed | Badge on the session row + a sentence in the panel | **Pipeline** column of `admin users` |
| Read another user's blueprint | Full viewer in `/admin` — tabs, zoom, Fit, 100%, pan, full screen | `admin diagrams <session-id>` (writes the PNGs; a terminal cannot show them inline) |
| Read another user's **architectural views** (SDD §3.2) | A second viewer under the blueprint in `/admin`, same three tabs and controls; shown once the session has a generated plan | `admin diagrams <session-id>` also writes `<out>/architecture/{logical,development,deployment}-view.png` |
| Another user's change history and follow-up scopes | Clickable rows with scope badges under the admin viewer; last scope on the collapsed row's chip | `admin diagrams <session-id>` prints the history with *applied to:*; `admin users` **Blueprint** cell shows the change count and last scope |
| Expand short brief | Interview path only | `interview expand` (wizard offers it too) |
| Docs path: statement + files + match check | Required + multi-upload (png/jpeg/pdf/docx/md/txt/csv/tsv/xlsx/xls; every sheet of a workbook is read) | `docs add … -s` |
| See which documents a project used | Docs step list + **Documents** chips in session details | `docs list` / `status` / `sessions` |
| **Process blueprint** (SIPOC / flow / swimlane PNGs) | **Blueprint** step — three tabs (SIPOC first) in a zoom / pan / full-screen viewer | `diagrams generate` (writes the three 3×-supersampled PNGs to `./blueprint`) |
| What each view is for | Purpose line on each empty-state tile, under the tab strip, and as the tab's tooltip | Legend printed by `diagrams show` / `generate` / `refine` / `save` |
| Follow-up on the diagrams | **Change something** box | `diagrams refine "..."` |
| **What a follow-up may change** | Two cards under the box — **All three diagrams** (revise the process) or **Only \<this view\>** (re-draw the picture) — and the choice is named on the spinner while it runs | `--scope all\|sipoc\|flow\|swimlane` on `diagrams refine`; `quickstart` asks the same question in its follow-up loop |
| Which changes moved the process | Green **All three views** / violet **\<view\> only** badge on every change-history entry | `diagrams show` prints *applied to:* under each entry |
| **A follow-up that changed nothing** | Amber note (not a red error), version unchanged, and your instruction is still in the box | `diagrams refine` prints the same advice in yellow and exits 1; `quickstart` prints it and asks again without leaving the loop |
| Approve / reopen the blueprint | **Approve blueprint** / **Reopen for changes** | `diagrams freeze` / `diagrams unfreeze` |
| Freeze gate before generate | Generate step refuses + **Review the blueprint** | `generate` returns 409 until `diagrams freeze` |
| Skip / discard the blueprint | **Skip to IDE** / **Discard** | Don't run `diagrams generate`, or `diagrams discard` |
| Blueprint change history | Listed under the diagrams | `diagrams show` |
| **See an earlier version's images** | Click the history entry → viewer shows that version, banner back to the current one | `diagrams save --version N` |
| Which versions can still be drawn | Entries say *diagrams not kept* and are not clickable | `diagrams show` marks them the same way |
| Swimlane stage names | The band across the top of the swimlane | `diagrams show` → **Stages** row |
| Every step and actor in full (untruncated) | **All steps and actors, in full** expander, with a filter box | `diagrams show` (Systems / Detail columns) |
| Approved diagrams in the zip | `docs/diagrams/<view>-v<N>.png` (images only) | Same zip · `diagrams save` writes the same filenames |
| **All three approved images after generate** | **Review → Diagrams** tab, full viewer | The PNGs `diagrams save` wrote |
| **Open an exported PNG from the file tree** | Click a `docs/diagrams/*.png` row → viewer, not the editor | Open the saved file |
| What each image is for, in the workspace | Diagrams tab intro + the tab titles | The generated `README.md`'s **Process blueprint** section (same text) |
| Live generate (Bedrock) | Always (no demo checkbox) | `generate` / `wizard` (live by default) |
| Demo generate (no Bedrock) | — | `generate --demo` only (offline tests) |
| Project-specific rules for every IDE | **Rules (N)** tab with the exported filename + apply mode | `plan` summary `Rules:` / `sessions` `#R` column and Rules names / `preview` `Rules:` line |
| **Source files** (app scaffold, `backend/` + `frontend/`) | **Source files** hint + **Files (N)** tab badge | `plan` summary `Source files:` / `preview` footer / `plan tree` |
| **Workspace files** (every exported path) | Export heading + tree toolbar + refresh message | `preview` footer `Workspace files:` |
| Full IDE tree (`.cursor` / `.claude` / `.windsurf`) | Files tab tree + Export | `preview` / zip |
| Export preview (tree + README) | Export workspace | `preview` / `plan tree` |
| Re-render workspace from the saved plan | **Refresh file tree** | Re-run `preview` |
| Expand / collapse tree folders | Click a folder row, or **Expand all** / **Collapse all** | `preview -l` (all) / `preview -l -d N` (collapse below depth N) |
| Delete session | Sessions Delete | `delete-session` |
| Wizard step back | **← Back** before generate only | `rollback` before generate only |
| No back after agents/skills/rules generated | Review / export locked | `rollback` refused after READY/EXPORTED |
| Detailed project README in zip | Yes (`README.md`) | Yes |
| Stack-aware `.gitignore` at root | Files tab + export (all IDEs) | `preview -f .gitignore` / zip |
| `CLAUDE.md` project memory at root | Files tab + export (Claude Code only) | `preview -f CLAUDE.md` / zip |
| Edit + save a generated root file | Select it, edit, **Save** (persists as override) | `plan edit --json` overrides |
| Phased work breakdown (`WORKBREAKDOWN.md`) | Files tab + export | `preview -f WORKBREAKDOWN.md` |
| Solution design document (`SDD.md`) | Files tab + export | `preview -f SDD.md` |
| **`SDD.md` as a PDF or a Word document** | **Download PDF** / **Download DOCX** wherever `SDD.md` is selected — the Review step's Files pane, the Export step's browser, and the Diagrams tab. Figures embedded, same markdown the pane shows | `plan document --format pdf\|docx\|both` writes the identical files (one parse, two writers) |
| **Download another user's design document** | The same two buttons under each session's figures in `/admin` | `admin document <session-id> --format …` |
| **Bedrock-generated detailed `WORKBREAKDOWN.md` and `SDD.md`** | Both started automatically when Review/Export opens — work breakdown first, then the design document — and polled every 4 s | Same: `preview`, `export` and `wizard` start both and wait (4 s poll, capped) before rendering or writing files |
| **The three architectural views** (`docs/architecture/*.png`) | Rows in the file tree that open in the diagram viewer — zoom, Fit, 100%, pan, full screen (Copy / Edit / Save hidden) — and the same PNGs inline in `SDD.md`'s own preview, fitted to the pane width and clickable | A `Diagrams: docs/architecture/` line naming the three files, written into `export`'s output beside `docs/diagrams/` |
| **Tables, `####` headings, links, rules and emphasis in a generated document** | Rendered — GFM pipe tables with alignment, headings to `######`, links through a URL allowlist, `---` thematic breaks, guarded `_italic_`. `npm run verify:markdown` checks it offline against a real `SDD.md` | Not applicable — `preview -f` prints the markdown source, which is what a terminal should show. The **downloaded** PDF/DOCX renders the same subset, from the same parser |
| Authenticated zip download | Yes (HttpClient blob) | Yes (Bearer on download) |
| Directory export | — (browser) | `export --out` |
| LLM config | — | `config show` / `set-model` |

---

## Docker (API only)

```bash
cp .env.example .env
docker compose up --build api
```

The published port comes from `API_PORT` in `.env` (default **8555**), so the CLI and UI
need no extra configuration — they derive the same URL. To reach a container on another
host, set `AGENTCRAFT_API_URL`.

---

## Backend tests

```bash
cd backend
# activate venv, PYTHONPATH=.
pytest -q
```

**207 tests**, no AWS credentials needed — Bedrock calls are stubbed. Tests run against a
throwaway SQLite file in the temp directory (`tests/conftest.py` sets `DATABASE_URL`), so
they never create, roll back, or delete rows in your real `agentcraft.db`.

Beside them, `backend/scripts/` holds the offline instruments for the parts a unit test reads
badly — a drawn diagram, a laid-out document, an end-to-end route. None needs a server, a
database, Bedrock or pytest:

```bash
cd backend
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/verify_solution_design.py   # the outline + the figures
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/_admin_arch_roundtrip.py    # routes, gating, both downloads, the CLI
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/_truncation_audit.py        # every way a sentence can be lost from a view
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/_document_export_sample.py  # a PDF + DOCX into .scratch/ to look at
cd ../frontend && npm run verify:markdown && npm run build                          # the preview subset, then the bundle
```

`PYTHONIOENCODING=utf-8` because these print `—`, `·` and `…`, which `cp1252` refuses. The two
underscore-prefixed round trips each leave a gitignored `_roundtrip.db` / `_truncation.db` behind
— delete them.

---

## Security

- Never commit `.env` or real AWS keys
- Treat `~/.agentcraft/token` like a password
- Change `JWT_SECRET` and super-admin password in production
- MCP exports use `${env:VAR}` placeholders only
- Only `API_PORT`, `FRONTEND_PORT` and `AGENTCRAFT_API_URL` reach the browser bundle —
  `scripts/write-env.cjs` writes those three values and nothing else, so credentials in
  `.env` stay server-side

---

## Project layout

- `backend/app` — API, auth, admin, exporters, Bedrock deduction
- `backend/app/services/diagrams` — process blueprint: `build.py` (extract / revise /
  reconcile), `render.py` (deterministic PNG layout), `store.py` (PNG cache),
  `architecture.py` (SDD §3.2's three views)
- `backend/app/services/export/documents.py` — one markdown parse, two writers: `SDD.md` as a
  PDF (`reportlab`) and as a DOCX (`python-docx`), rendering the same subset the UI previews
- `diagrams/<project-id>/` — rendered PNG cache; safe to delete, re-rendered on demand
  (`architecture/` is a subfolder of it, and just as safe)
- `backend/scripts` — offline instruments, no pytest and no server: `verify_solution_design.py`,
  `_admin_arch_roundtrip.py` (the real ASGI app in-process), `_arch_sample.py` and
  `_document_export_sample.py` (draw / lay out into gitignored `backend/.scratch/` so a human can
  look), `_truncation_audit.py` (every string shortened, shortened upstream, or drawn over, with
  the call site)
- `frontend/src` — wizard, sessions, login/signup, admin dashboard
- `frontend/scripts` — `read-env.cjs` (parse root `.env`), `write-env.cjs` (generate
  `environment.ts`), `serve.cjs` (`ng serve` on `FRONTEND_PORT`)
- `cli/agentcraft` — `agentcraft` CLI entrypoint
- `backend/run_api.ps1` / `run_api.sh` — start the API on the host + port from `.env`
- `run-tmux.sh` — start backend + UI in one tmux session; see [TMUX.md](TMUX.md)
- `deploy/supervise.sh` — keeps one service alive in a tmux pane and reports why it exited
  (signal, uptime, memory, any OOM record); used by both panes of `run-tmux.sh`
- `deploy/install-service.sh` — install backend + UI as systemd services (restart on crash,
  start at boot) for a box that should stay up
