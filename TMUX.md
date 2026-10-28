# Running AgentCraft under tmux

Keeping the backend and UI alive after you close an SSH session. Four options — pick one and
use its own start / attach / kill commands together, since each names its sessions
differently.

Ports come from the repo-root `.env` (`API_PORT=8555`, `FRONTEND_PORT=4225`). The UI resolves
the API host from the browser's address, so the same commands work locally and on EC2 with
nothing to change — see the README's *The same setup runs locally and on EC2*.

Paths below assume the repo at `~/ST-V3/AgentCraft-HexaAgent`. Adjust if yours differs.

---

## Contents

- [Install tmux](#install-tmux)
- [Option A — the run-tmux.sh script](#option-a--the-run-tmuxsh-script)
- [Option B — two sessions, one per service](#option-b--two-sessions-one-per-service)
- [Option C — one session, two panes](#option-c--one-session-two-panes)
- [Option D — fully detached one-liners](#option-d--fully-detached-one-liners)
- [Without run_api.sh](#without-run_apish)
- [After a git pull that adds dependencies](#after-a-git-pull-that-adds-dependencies)
- [tmux cheat sheet](#tmux-cheat-sheet)
- [Startup failures that look worse than they are](#startup-failures-that-look-worse-than-they-are)
- [When a pane says Terminated](#when-a-pane-says-terminated)
- [Freeing a stuck port](#freeing-a-stuck-port)
- [Open the app](#open-the-app)
- [Use the CLI on the box](#use-the-cli-on-the-box)
- [Keeping it up without tmux (systemd)](#keeping-it-up-without-tmux-systemd)

---

## Install tmux

```bash
sudo apt update && sudo apt install -y tmux    # Ubuntu / Debian
tmux -V                                        # confirm
```

---

## Option A — the `run-tmux.sh` script

One session named `agentcraft`, backend in the left pane, UI in the right. Each pane keeps a
shell if its process dies, so a traceback stays on screen instead of vanishing with the pane.

**Both panes now restart themselves.** Each one runs its service under a small supervisor, so a
process that is stopped by something outside it comes back instead of leaving the pane at a bare
prompt — and every exit prints why it happened. See
[When a pane says `Terminated`](#when-a-pane-says-terminated) for what that output means.

**Start**

```bash
cd ~/ST-V3/AgentCraft-HexaAgent
./run-tmux.sh
```

Re-running it attaches to the existing session rather than starting a second copy on the
same ports.

**It finds the virtualenv itself** — you no longer have to activate one first. It looks in
`.venv`, `backend/.venv`, `venv` and `backend/venv` (both `bin/activate` and `Scripts/activate`),
and prints which one it used:

```
Started tmux session 'agentcraft' (backend :8555 · UI :4225)
  venv   : /home/ubuntu/ST-V3/AgentCraft-HexaAgent/.venv
```

If it finds none it says so on stderr and gives you the `python3 -m venv` line, rather than
starting uvicorn on the system python and failing later with `ModuleNotFoundError: sqlalchemy`.
Set `AGENTCRAFT_VENV=/path/to/venv` to point it somewhere else.

**Attach**

```bash
tmux attach -t agentcraft
```

`Ctrl-b` `o` switches pane · `Ctrl-b` `d` detaches and leaves both running.

**Kill**

```bash
./run-tmux.sh stop
# same thing by hand — note the `=`:
tmux kill-session -t =agentcraft
```

It stops the session named exactly `agentcraft` and prints what it left running, so on a box that
hosts several tmux stacks you can see nothing else was touched.

The `=` matters if you do it by hand. `-t agentcraft` is a *loose* target: tmux tries an exact
name, then any session that name is a prefix of, then a glob — so with no exact `agentcraft`
session it will cheerfully kill `agentcraft-old` or `agentcraft2`, i.e. somebody else's work, and
report success. `=agentcraft` means that name and nothing else. `./run-tmux.sh stop` does this for
you (it used to have exactly this bug), and so do attach and the pane commands.

---

## Option B — two sessions, one per service

Separate sessions mean separate logs and independent restarts — restart the UI without
touching the API. The most practical option day to day.

**Start**

```bash
# backend
tmux new -s api
cd ~/ST-V3/AgentCraft-HexaAgent/backend
source .venv/bin/activate
./run_api.sh
#   Ctrl-b  then  d      to detach

# frontend
tmux new -s ui
cd ~/ST-V3/AgentCraft-HexaAgent/frontend
npm start
#   Ctrl-b  then  d      to detach
```

**Attach**

```bash
tmux attach -t api        # backend logs
tmux attach -t ui         # UI build output
tmux ls                   # what is running
```

**Kill**

```bash
tmux kill-session -t api        # backend only
tmux kill-session -t ui         # UI only
tmux kill-session -t api \; kill-session -t ui    # both at once
```

---

## Option C — one session, two panes

Both logs visible side by side.

**Start**

```bash
tmux new -s agentcraft

# left pane — backend
cd ~/ST-V3/AgentCraft-HexaAgent/backend && source .venv/bin/activate && ./run_api.sh

#   Ctrl-b  then  %      split vertically

# right pane — UI
cd ~/ST-V3/AgentCraft-HexaAgent/frontend && npm start

#   Ctrl-b  then  d      detach; both keep running
```

**Attach**

```bash
tmux attach -t agentcraft
```

**Kill**

```bash
tmux kill-session -t agentcraft          # whole session, both services
```

To stop just one service, focus its pane (`Ctrl-b` `o`) and press `Ctrl-c`.

---

## Option D — fully detached one-liners

Nothing to attach to, nothing to type twice. Good for a deploy script or a reboot hook.

**Start**

```bash
tmux new -d -s api 'cd ~/ST-V3/AgentCraft-HexaAgent/backend && source .venv/bin/activate && ./run_api.sh'
tmux new -d -s ui  'cd ~/ST-V3/AgentCraft-HexaAgent/frontend && npm start'
```

**Attach**

```bash
tmux attach -t api
tmux attach -t ui
```

> A session started this way **ends when its command exits**, so if the process crashes the
> session disappears and takes the error with it. Use Option A or B when you need to see why
> something died.

**Kill**

```bash
tmux kill-session -t api
tmux kill-session -t ui
tmux kill-session -t api \; kill-session -t ui    # both
```

---

## Without `run_api.sh`

If you have not pulled the script yet:

```bash
cd ~/ST-V3/AgentCraft-HexaAgent && git pull
chmod +x backend/run_api.sh run-tmux.sh
```

Or run uvicorn directly — note **`--host 0.0.0.0`**:

```bash
cd ~/ST-V3/AgentCraft-HexaAgent/backend
source .venv/bin/activate
export PYTHONPATH=.
uvicorn app.main:app --reload --host 0.0.0.0 --port 8555
```

> **Do not use `--host 127.0.0.1` on a server.** It binds loopback only: the startup log
> looks perfectly healthy and every connection from outside the machine is refused.
> `run_api.sh` reads `API_HOST` from `.env` (`0.0.0.0`) and warns if it is loopback.

---

## After a `git pull` that adds dependencies

A pull is not always enough. The process blueprint round added a Python package, a database
column and new response fields, so on an existing box:

```bash
cd ~/ST-V3/AgentCraft-HexaAgent && git pull

# 1. new Python dependency (matplotlib — renders the blueprint PNGs)
cd backend && source .venv/bin/activate && pip install -r requirements.txt

# 2. new column. SQLite adds it on startup; Postgres does not:
#    psql "$DATABASE_URL" -c 'ALTER TABLE projects ADD COLUMN diagrams_json TEXT;'

# 3. restart the API — response schemas changed, so --reload alone is not enough
#    if the process was started before the pull
tmux kill-session -t api && tmux new -d -s api 'cd ~/ST-V3/AgentCraft-HexaAgent && ./backend/run_api.sh'

# 4. rebuild the UI (new Blueprint wizard step)
cd ../frontend && npm install && npm start
```

`matplotlib` needs no system packages: it ships its own fonts and the **Agg** backend draws
without a display, so a headless EC2 box needs nothing beyond `pip install`. The rendered
PNGs are a cache under `diagrams/` — deleting that folder is safe.

The diagram-viewer round needs the same two steps and nothing else:

- **Restart the API.** The blueprint response gained a per-image `scale` field, and the PNG
  cache filenames changed (`<kind>-v<version>-r<revision>-s<scale>.png`), so a process that
  started before the pull serves the old shape.
- **Rebuild the UI.** The viewer (zoom / pan / full screen) and the full step list are new
  front-end code, and the viewer needs `scale` to know what 100% means.

No migration and no cache clearing: the renderer's revision is part of each filename, so
existing projects re-render themselves on the next request. Old files are deleted by the
same prune that already ran for superseded versions.

The HD / interactive-diagrams round is the same two steps again:

- **Restart the API.** The renders are now 3× supersampled with orthogonally routed
  handoffs (`RENDER_REVISION` bumped), the PNG endpoint takes `?version=` so the UI can show
  an earlier version from the change history, and each revision now reports `viewable`.
- **Rebuild the UI.** SIPOC is the landing tab, change-history entries are clickable, the
  step list has a filter, and the viewer has keyboard shortcuts.

Still no migration and nothing to prune by hand. Two things to expect on the box:

- **A first draw takes a few seconds longer.** Three views at 3× is ~10–16 s of CPU for a
  25-step process (it was ~4 s at 2×). It runs in the background job the wizard polls, so
  nothing times out, but a `top` during a draw will show one busy core.
- **The PNGs are bigger on disk** — roughly 2–3× the bytes for the same diagram. Still under
  ~1 MB each, and still a cache: `rm -rf diagrams/` is safe at any time.

The exported-diagrams round is the same two steps once more:

- **Restart the API.** `docs/diagrams/` in the export changed shape: it now holds the approved
  version's three PNGs named `<view>-v<N>.png` and nothing else — `process-model.json` and the
  per-folder `README.md` are gone, and what each image is for is written into the workspace's
  own `README.md` instead. `/export/preview` also returns a new `images` array.
- **Rebuild the UI.** Review has a new **Diagrams** tab, and a `docs/diagrams/*.png` row in
  either file tree opens the image in the viewer rather than an empty editor.
- **Reinstall the CLI** only if it is installed non-editably: `agentcraft diagrams save` now
  writes the same versioned filenames as the zip (`sipoc-v2.png`, not `sipoc.png`).

Nothing to migrate. Old zips already sitting in `exports/` still contain the previous layout:
the **Download** button re-exports before it downloads, so it always hands over a fresh zip,
but a direct `GET /export/download` serves the cached file if one is there. `rm -rf exports/`
if you want to be certain — they are rebuilt on demand.

The header round is **restart the API only** — no UI rebuild, no migration:

- The scope sentence under each diagram's title used to be sliced at 150 characters and drawn
  as one line the canvas edge then cut again; it now wraps, and the header is a tinted band
  with a rule under it so the prose cannot read as part of the drawing.
  `RENDER_REVISION` went to **7**, so every existing project redraws on its next request and
  the stale files are pruned as usual. Nothing to clear by hand.
- **A renderer edit without a revision bump serves a stale PNG.** That is the whole point of
  the number being in the filename, and it is easy to change the drawing twice under one
  revision and then wonder why the fix has not appeared. If a diagram looks like code you
  already changed, check `RENDER_REVISION` before anything else.

The viewer fix that went with it **does** need a UI rebuild:

- Full screen no longer distorts a tall diagram. `width: 100%` plus `max-height: 100%` squashed
  a 936 × 3088 flow to the width of the screen and a few hundred pixels tall; the width is now
  `auto` with both axes bounded.
- All three views open **fitted**, and **full screen switches to 100%** (leaving it goes back to
  fit). Driven by `fullscreenchange`, so Esc and F11 behave like the button.
- Already-exported zips hold the old PNGs. Re-export (or press **Download**, which re-exports
  first) to get the corrected images.

The layout-width round is **UI rebuild only** — no API restart, no migration:

- The wizard, session board and admin console were capped at 1080 / 1180 / 1080 px and now
  share `--shell-max: 1600px` from `frontend/src/styles.css`. On a 1440p or wider screen that
  reclaims the empty third on either side; on anything narrower nothing changes, because the
  cap was never the limit there.
- The file browsers give the reclaimed width to the file, not the tree, and the tree / editor /
  preview panes are now `min(64vh, 680px)` tall.
- `npm start` picks this up on save — it is CSS only. A box serving `frontend/dist/` from nginx
  needs `npm run build` before the change is visible.

The admin-panel round is **API restart + UI rebuild** — still no migration:

- `/admin/users` sessions gained the pipeline fields (`path`, `path_inferred`,
  `interview_answer_count`) and blueprint metadata (`has_diagrams`, `diagrams_frozen`,
  `diagram_version`, `diagram_title`, and the step / actor counts). A response schema changed,
  so `--reload` alone is not enough — restart the API.
- Two new admin-gated read-only routes:
  `GET /api/v1/admin/projects/{id}/diagrams` and `…/diagrams/{kind}.png`. They exist because
  every `/projects/**` handler filters by owner and **404s on another user's project** by
  design, which left the one reader who cannot open the wizard with no way to see a diagram.
  Read-only: an admin can look, never redraw, edit or approve.
- The panel's metadata comes from `projects.diagrams_json` parsed directly, **not** from
  `project_service.diagrams()`. That call renders all three PNGs to report their pixel sizes,
  so routing `/admin/users` through it would fire a matplotlib draw per session per request.
  If you add a field there, read the JSON — do not "just reuse the service".
- The viewer is now its own component (`frontend/src/app/shared/diagram-viewer/`), mounted by
  the wizard's four call sites and the admin panel. One copy of the zoom / fit / 100% / pan /
  full-screen behaviour, so the admin sees what the user sees rather than a lookalike.
- No new columns, so nothing to `ALTER`. `npm run build` is required on a box serving
  `frontend/dist/`.

The follow-up-scope round is **API restart + UI rebuild** — still no migration:

- A follow-up now says what it may change. `POST /diagrams/followup` takes `scope` in the body
  (`all` | `sipoc` | `flow` | `swimlane`), each change-history revision records the `scope` it
  was applied with, and `/admin/users` sessions gained `diagram_revision_count` and
  `diagram_last_scope`. Response schemas changed, so restart the API rather than relying on
  `--reload`.
- **Legacy revisions read as `all`,** which is what they were: before this, every follow-up
  revised the process and redrew all three views. The field defaults on load, so nothing needs
  backfilling and no column was added.
- **`scope != "all"` does not touch the process model.** It re-runs one view's layout call with
  the instruction as presentation guidance, then reconciles the answer against the *unchanged*
  model — a layout that invents or drops a step has it repaired and the repair lands in
  `warnings`. The other two views are untouched, so nothing can end up contradicting anything.
  Anything that must change what the process *is* has to go through `scope="all"`, because the
  model is what generation reads.
- **A view-scoped follow-up copies two PNGs instead of redrawing them.** `render_set` takes
  `redraw=` and `reuse_from=`, and the caller decides from the data — it compares the stored
  views and the model, not the scope the client asked for — so a single-view change costs one
  render rather than three (a 25-step swimlane at 3× is ~10 s of a core). A mismatch cannot
  serve a stale image because the comparison, not the request, decides. Still a cache:
  `rm -rf diagrams/` is safe.
- Either scope **reopens** an approved blueprint, and both are refused with **409** while it is
  frozen. One rule: the approval was of a blueprint the user was looking at, and a re-drawn view
  changes what they would be looking at.
- **Reinstall the CLI** only if it is installed non-editably: `agentcraft diagrams refine` gained
  `--scope` (alias `--only`), `diagrams show` prints *applied to:* per history entry, the
  `admin users` **Blueprint** cell shows the change count and last scope, and `quickstart`'s
  follow-up loop asks the same question.
- `npm run build` on a box serving `frontend/dist/` — the scope chooser, the scope-aware spinner
  and the admin panel's clickable change history are all front-end code.

The follow-up-actually-applies round is **API restart + UI rebuild** — no migration, no new
columns, and a **new prompt file that must be deployed with the code**:

- **New file: `backend/app/prompts/diagram_view_followup_system.txt`.** It is appended to each
  view's drawing prompt on the revision path only. A deploy that ships `build.py` without it
  fails the follow-up at the first call (`_prompt()` reads from disk), so check it is in the
  image or the checkout.
- **Why it exists.** Each view's drawing prompt is written for a *first* draw and is deliberately
  conservative ("use the model's exact vocabulary"), so a view-scoped follow-up asking to re-word
  something was read as forbidden and answered with a clarifying question instead of applied. The
  addendum says what a revision may rewrite per view: on a SIPOC every column's entries and notes
  (they exist nowhere in the process model, so nothing else draws them); on the flow node labels,
  detail lines, systems captions, edge labels and the title; on the swimlane step labels, lane
  order, column grouping and stage names. Step ids, owners and topology stay with the model.
  Restart the API — prompts are read per call, but `build.py` changed too.
- **A follow-up that changes nothing no longer records a version.** The layout call is retried
  once with its own non-answer quoted back; if the second answer is identical too, the request
  ends as **422** whose message opens with the literal words `Nothing changed`. The wizard and the
  CLI both test for that opening to show it amber rather than red, so if you reword those messages
  keep the first two words. On `wait=false` the same text goes to the project's `error` column, so
  the poll surfaces it unchanged — there is no "Blueprint follow-up failed:" prefix on this path.
- **Nothing is written on that path**: same version, same `frozen` flag, same PNG cache. A stuck
  follow-up therefore costs two layout calls and leaves no trace to clean up.
- **Reinstall the CLI** only if it is installed non-editably: `diagrams refine` now prints the
  advice in yellow and exits 1 instead of red-and-exit, and `quickstart` stays in its follow-up
  loop so the user can re-say it more concretely.
- `npm run build` on a box serving `frontend/dist/` — the amber banner and keeping the typed
  instruction in the box after a no-op are front-end code.

The solution-design round (`SDD.md` + `docs/architecture/`) is **API restart + UI rebuild** —
no migration, no new columns, and **four new prompt files that must be deployed with the code**:

- **New prompt files: `backend/app/prompts/solution_design_{single,scope,solution,delivery}.txt`.**
  Prompts are read from disk per call, so a deploy that ships the new modules without them fails
  the document at the first call. Check all four are in the image or the checkout.
- **New modules:** `app/services/deduction/solution_design.py` (the renderer + validator) and
  `app/services/diagrams/architecture.py` (the three views' layout engine).
- **No schema change.** `solution_design`, `solution_design_llm`, `solution_design_complete` and
  `architecture_views` are fields on the **plan JSON**, not columns — every existing project
  picks the document up on its next preview / refresh / export with nothing run by hand.
- **New route:** `GET /api/v1/projects/{id}/plan/architecture/{kind}.png` and
  `POST /api/v1/projects/{id}/plan/solution-design/generate`. Both use the ordinary
  `get_current_user` scoping — the design document is **not** role-gated.
- **The PNG cache gained a subfolder:** `diagrams/<project-id>/architecture/`. Still a pure
  cache — `rm -rf diagrams/` is safe — and the subfolder is deliberate: blueprint pruning globs
  `*.png` non-recursively and would delete the digest-named architecture files.
- **Rebuild the UI** — `npm run build` on a box serving `frontend/dist/`. The architecture image
  pane and the markdown-preview fixes (GFM tables, headings past `###`, links, the three inline
  figures) are all front-end code, and a stale bundle shows a generated document's tables as
  rows of literal pipes.
- **Reinstall the CLI** only if it is installed non-editably: `preview` / `export` / `wizard` now
  wait for `SDD.md` as well as `WORKBREAKDOWN.md`, and `export` prints a `Diagrams:` line for
  `docs/architecture/`.
- **Checks, both offline** — no Bedrock, no database, no pytest:
  `backend/.venv/Scripts/python.exe scripts/verify_solution_design.py` and, in `frontend/`,
  `npm run verify:markdown`.

The detailed-diagrams follow-up is **API restart + UI rebuild** on the same round — no
migration, no new files, and nothing to clear by hand:

- **`ARCH_REVISION = 3` in `app/services/diagrams/architecture.py`.** It is part of the cached
  filename, so every architecture PNG on disk is stale the moment this deploys and redraws on
  first request. Nothing to delete; the old digest-named files are pruned as they are replaced.
- **The two solution prompts changed shape,** not just wording:
  `solution_design_solution.txt` and `solution_design_single.txt` now ask for `components` and
  `hosts` as objects (`name` / `tech` / `detail`), `connections` with `port` and `data`, and
  `key_files` per module. Deploy them with the code — a stale prompt still validates, it just
  draws one-line boxes.
- **Old payloads keep working.** `architecture_views` stored before this round holds plain
  strings; both the renderer and the tables accept either, so no project needs regenerating and
  the fields stay on the plan JSON. Regenerating a design is what upgrades a picture.
- **Rebuild the UI** — SDD.md gained tables (**Components in detail**, **Processes in detail**,
  the `Port` / `Carries` hop columns) and the pane needs the current table CSS and heading
  anchors to render them.

The full-width-figures follow-up is a **UI rebuild only** — no API restart, no prompt, no cache:

- `npm run build` on a box serving `frontend/dist/`. Everything in it is front-end: the
  `docs/architecture/*.png` rows now open in the shared `ac-diagram-viewer` (fit / 100% /
  drag-to-pan / full screen) instead of a plain `<img>` capped at 62vh, and SDD.md's inline
  figures are fitted to the pane width instead of overflowing it sideways at their 3× size.
- **Nothing server-side moved** — same endpoint, same bytes, same `ARCH_REVISION`. A box that
  only serves the API needs no action at all.
- **Check:** `npm run verify:markdown` in `frontend/` (two new assertions — the figure's click
  affordance, and that a lone figure is still wrapped in a `<p>`, which is what the width rule
  is written against) and `npm run build`, which is where a template or CSS mistake surfaces.

The admin-architecture + richer-diagrams round is **API restart + UI rebuild** again — unlike
the UI-only round above, this one moves the server. No migration and no new columns:

- **`ARCH_REVISION` 3 → 4.** It is part of the digest in the cached filename, so **every
  architecture PNG on disk is stale the moment this deploys** and redraws on first request.
  Nothing to delete by hand; the old digest-named files are pruned as they are replaced. The
  first request per view after a deploy therefore pays a draw — if a page looks slow once and
  fast afterwards, this is why. And the standing warning applies here too: **a renderer edit
  without a revision bump serves the old picture**, which is easy to do twice in a row and then
  wonder why the fix has not appeared.
- **New admin route:** `GET /api/v1/admin/projects/{id}/architecture/{logical|development|
  deployment}.png` — the read-only twin of the owner's `…/plan/architecture/{kind}.png`, so an
  admin can see SDD §3.2's three figures for a session they do not own. No `version` parameter:
  these draw the current design. `/admin/users` sessions also gained **`has_plan`**, a response
  schema change, so restart the API rather than relying on `--reload`.
- **`has_plan` is a column read, not a render.** It is `bool(row.plan_json)` for the same reason
  the blueprint metadata is parsed JSON: `architecture_png` draws with matplotlib, so deciding
  it per session inside `/admin/users` would fire nine renders per user per request.
- **The two solution prompts changed shape again** —
  `solution_design_solution.txt` and `solution_design_single.txt` now ask for an `interface` per
  logical component, a `state` per deployment host, `key_files` 3–5 per package, and a package
  responsibility that names what the package is *not* allowed to do. Deploy them with the code;
  a stale prompt still validates, it just draws a thinner fourth line or none.
- **Old payloads keep working.** A design stored before this round has no `interface` / `state`,
  so those boxes draw three lines as they did — the picture is redrawn, not regenerated.
  Regenerating a design is what fills the fourth line in.
- **Rebuild the UI** — `npm run build` on a box serving `frontend/dist/`. The admin panel now
  mounts a **second** `ac-diagram-viewer` under the blueprint for the architecture views, and
  the two share `keyboardOwner` so the bare keys (`f`, `0`, `1`, `+`, `-`, arrows) reach only
  the figure under the pointer. `ARCHITECTURE_VIEWS` / `ARCHITECTURE_RENDER_SCALE` moved into
  the shared viewer's own file — if you add a view, add it there, not in the wizard.
- **Reinstall the CLI** only if it is installed non-editably: `admin diagrams <session-id>` now
  writes `<out>/architecture/{logical,development,deployment}-view.png` as well, and no longer
  exits early when the session has no blueprint — the two are independent.
- **SDD.md's tables gained columns** (*Exposes / contract* in §3.2.1, *Imported by* in §3.2.2,
  *State* in §3.2.3), and the generated workspace's `README.md` gained an **Architecture
  (`docs/architecture/`)** section. Both are re-derived on the next preview / export; no stored
  document needs touching.
- **Checks, all offline** — no Bedrock, no database, no pytest:
  `backend/.venv/Scripts/python.exe scripts/verify_solution_design.py`, and in `frontend/`
  `npm run verify:markdown` plus `npm run build`. To *look* at the three figures rather than
  assert on them, `backend/.venv/Scripts/python.exe scripts/_arch_sample.py` draws them from a
  realistic payload into `backend/.scratch/` (gitignored) — the only way to catch a clipped line
  or a collided label, since there is no headless browser on these boxes.
- **The end-to-end check needs no server either.**
  `backend/.venv/Scripts/python.exe scripts/_admin_arch_roundtrip.py` drives the real ASGI app
  in-process with `TestClient` against a throwaway SQLite file: it signs in the super admin,
  signs up and approves an owner, walks the whole FSM (path → interview → platform →
  `generate?demo=true&wait=true`) and then asserts the admin gets three real PNGs for a session
  they do not own, that those bytes match what the owner's own route serves, that the owner is
  403 and an anonymous caller 401 on the admin route, that `/admin/users` reports
  `has_plan: true`, and that the export preview carries `docs/architecture/` plus the README
  section describing it. It reads the admin credentials from settings and never prints them.
  The interview is finished in one POST by answering `goal` with a structured expanded brief —
  three or more `##` sections — which is the branch that prefills the optional answers without
  calling Bedrock. Delete the `backend/_roundtrip.db` it leaves behind (`*.db` is gitignored, so
  a forgotten one is untracked rather than committed).

The document-download round is **API restart + UI rebuild + `pip install`** — the first round
here that adds a Python dependency, and the one to read carefully because two revision numbers
moved at once:

- **`pip install -r backend/requirements.txt` before restarting.** The round adds
  **`reportlab>=4.2.0`** (`backend/requirements.txt`), which is what lays the PDF out. A deploy
  that ships the code without it starts fine and then 500s the first time somebody clicks
  **Download PDF** — the import is inside `app/services/export/documents.py`, so nothing fails
  at boot. `python-docx` was already a dependency; the DOCX half needs no new package.
- **`ARCH_REVISION` 4 → 8 and `RENDER_REVISION` 7 → 10.** Both are part of the cached filename,
  so **every PNG on disk — blueprint and architecture alike — is stale the moment this deploys**
  and redraws on first request. Nothing to delete by hand. This is the widest cache turnover of
  any round so far: the first request per view after a deploy pays a draw, which is why a page
  can look slow once and fast for everyone afterwards.
- **What moved the drawing:** every string either renderer shortens is now audited rather than
  eyeballed (`scripts/_truncation_audit.py`), and the two defects it found are fixed — an
  architecture payload cell was sliced at a character count *before* the renderer, so nothing
  marked the cut and boxes read `"…and the error shape. Deleg"`; and the diagram **title** was
  clipped on the narrow flow canvas, so a project named after its own summary lost the end of it.
  Titles wrap to a second line now, with the header band and the whole body measured for it. The
  audit reports **0 cuts** on all six views, which is the state to keep it in.
- **New routes, two of them, four URLs:**
  `GET /api/v1/projects/{id}/plan/solution-design.{pdf|docx}` for the owner and
  `GET /api/v1/admin/projects/{id}/solution-design.{pdf|docx}` for a super admin, the read-only
  twin. Both serve `Content-Disposition: attachment` with a project-named filename, and both go
  through **one** service method (`project_service.solution_design_document`), so an admin
  downloading a user's design is reading that user's document and not a re-generation of it. An
  unknown extension is a **404**, not a 500. Neither route calls Bedrock: a design that has
  never been generated is backfilled deterministically, exactly as the PNG routes do it, because
  a Bedrock call behind a download button leaves the reader watching a spinner.
- **New module: `app/services/export/documents.py`** — one markdown parse, two writers. Deploy
  it with the code; there is no prompt file and no schema change in this round.
- **No migration, no new columns, no new folder.** The documents are laid out per request and
  never cached, so there is nothing to prune and nothing to clear.
- **Rebuild the UI** — `npm run build` on a box serving `frontend/dist/`. Front-end in this
  round: the **Download PDF / Download DOCX** buttons (both file browsers when `SDD.md` is the
  selected row, and the Diagrams tab under the architecture filename chips), the same pair in
  the **admin panel** under each session's figures, the Diagrams tab's count fixed to match the
  views it actually renders, and clicking `SDD.md` in that tab now selecting the file. A stale
  bundle shows the old count and no buttons at all.
- **The preview learned two more marks** — `---` / `***` / `___` thematic breaks and guarded
  `_italic_` — because the exporter renders them and a document has to read the same in the pane
  as it does on paper. Guarded: `batch_service.py` and `snake_case_name` are everywhere in these
  documents and none of them is emphasis.
- **Reinstall the CLI** only if it is installed non-editably: two new commands,
  `agentcraft plan document --format pdf|docx|both` and `agentcraft admin document <session-id>`,
  which write the same files the browser downloads (the filename comes from the response header,
  not from the CLI).
- **Checks, all offline** — no Bedrock, no server, no pytest:
  `backend/.venv/Scripts/python.exe scripts/_admin_arch_roundtrip.py` now also asserts both
  formats download from both sides, that the admin's copy is the same *document* as the owner's
  (compared by extracted text and image digests, not bytes — a PDF carries `/CreationDate` and a
  DOCX a modification time per zip entry, so two renders of one document are never byte-equal),
  that the figures are embedded in both, that the gating matches the PNG routes, and that the
  CLI's own helper writes the files. Then `scripts/verify_solution_design.py`,
  `scripts/_truncation_audit.py` (sweeps the drawn views for a label the renderer cut), and in
  `frontend/` `npm run verify:markdown` plus `npm run build`. To *look* at a document rather than
  assert on it, `scripts/_document_export_sample.py` writes a PDF and a DOCX into
  `backend/.scratch/` (gitignored). `_truncation_audit.py` leaves a `backend/_truncation.db`
  behind — delete it; like `_roundtrip.db` it is untracked, not committed.

The readable-views round is the cheapest kind: **API restart only**. No new dependency, no
migration, no new column, no route change, no front-end change — a stale UI bundle serves it
correctly, because everything that moved is inside the two renderers.

- **`RENDER_REVISION` 10 → 11 and `ARCH_REVISION` 8 → 9.** Both are in the cached filename again,
  so every PNG on disk is stale on deploy and redraws on first request. Nothing to delete by hand,
  and the same one-slow-request-then-fast-for-everyone behaviour as the last round.
- **What moved the drawing.** The previous round's line budgets had been sized against the short
  demo project; a real one overran nearly all of them, so a step's detail lost the clause naming
  the requirement it satisfies, a systems caption lost the second of its two services, a branch
  label lost its condition, and a lane's responsibilities were dropped whenever the lane was
  short. Every budget is now set from the widest text a real plan produces. One of them was not a
  budget at all: `room` for the detail came back as `5.999999999999999` lines for the six the
  height pass had just paid for, so floor division cut the sentence that had decided the height of
  every box on the page.
- **And then the same defect without a truncation.** With nothing cut, what was left was text made
  unreadable by something drawn over it — which costs the reader the same words. An edge label
  sits on an opaque plate, so two in one place lose *both*, and a plate through the middle of a
  box's sentence reads exactly like a cut. Every string drawn is now recorded rather than just the
  labels, a label prefers shape-free space, then text-free space, then further out, and past
  roughly its own size from its arrow the attachment is **drawn as a dotted leader** instead of
  left to proximity — a label 269px off its own arrow on an 896px page belongs to no arrow at all.
  Three leaders across six views; if a view grows a cluster of them, some corridor is too tight.
- **The audit grew to match.** `scripts/_truncation_audit.py` now also watches for a cut applied
  *before* the renderer (`_wrap` is blind to those — the text it is handed already ends in
  somebody else's ellipsis) and for a label drawn over any string on the page, and it exits
  non-zero on either, and on stored prose that ends mid-sentence. Current state: **0 cuts, 0
  labels over text, 0 adrift** on all six views, which is the state to keep it in.

---

## tmux cheat sheet

```bash
tmux ls                              # list sessions
tmux attach -t <name>                # attach
tmux new -s <name>                   # new session
tmux new -d -s <name> '<command>'    # new detached session running a command
tmux kill-session -t <name>          # kill one
tmux kill-server                     # kill every session (nuclear)
```

`Ctrl-b` is the prefix — press and release it, *then* the key.

| Keys | Action |
| --- | --- |
| `Ctrl-b` `d` | Detach — session keeps running |
| `Ctrl-b` `%` | Split vertically (side by side) |
| `Ctrl-b` `"` | Split horizontally (stacked) |
| `Ctrl-b` `o` | Next pane |
| `Ctrl-b` `←` `→` `↑` `↓` | Move between panes |
| `Ctrl-b` `x` | Kill the current pane (confirm `y`) |
| `Ctrl-b` `[` | Scroll back through output (`q` to exit) |
| `Ctrl-b` `z` | Zoom the current pane full-screen (again to undo) |
| `Ctrl-c` | Stop the process in the focused pane |

---

## Startup failures that look worse than they are

Either pane can die within a second of `./run-tmux.sh`, with a long stack trace that names a
file deep inside a library and never names the fix. None of these is an application bug.

### UI: `Cannot find module '.../node_modules/@angular/cli/bin/ng.js'`

`node_modules` was never installed, or an interrupted install left it half-written. The
`[env] ng serve on …` lines print first, so it reads as though the app started and then broke.

```bash
cd ~/ST-V3/AgentCraft-HexaAgent/frontend
npm ci          # or: npm install   (no package-lock.json)
npm start
```

`run-tmux.sh` now checks for that exact file and runs the install itself when it is missing,
so this should not recur on a fresh clone. If an install is what fails, `rm -rf node_modules`
and retry — `npm ci` replaces the tree, `npm install` patches it.

### UI: `The Angular CLI requires a minimum Node.js version of v22.22.3`

The install succeeded — 300-odd packages, a wall of yellow `EBADENGINE` warnings — and then
`ng serve` exited. **The dependencies are fine; the runtime is too old.** Angular 22 requires
Node **v22.22.3, v24.15.0 or v26.0.0** and refuses anything else, including the odd-numbered
majors. Ubuntu 22.04's `apt` node is v18, so a box that was never given a newer one lands here.

`npm start` now stops before Angular with the upgrade commands (`frontend/scripts/check-node.cjs`),
`package.json` declares the requirement in `engines`, and `run-tmux.sh` warns before it starts
the pane. Upgrading is the only fix:

```bash
# nvm — per-user, so it cannot break another project on the same box that still needs Node 18
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash
export NVM_DIR="$HOME/.nvm" && . "$NVM_DIR/nvm.sh"
nvm install --lts && nvm use --lts && nvm alias default "lts/*"
node --version                       # must be v22.22.3, v24.15.0, v26.0.0 or newer

cd ~/ST-V3/AgentCraft-HexaAgent/frontend
rm -rf node_modules && npm ci        # rebuild against the new Node
npm start
```

System-wide instead, if this box only runs AgentCraft:

```bash
curl -fsSL https://deb.nodesource.com/setup_24.x | sudo -E bash -
sudo apt-get install -y nodejs
```

`--lts` rather than a major on purpose: `nvm install 22` resolves to the newest 22.x, which is
only good enough while that happens to be ≥ v22.22.3. Verify with `node --version` — if it still
prints v18, the shell has not picked the new one up.

Two things to know: `nvm use` applies to **that shell only**, so `nvm alias default` is what
makes a fresh tmux pane get it too — check with `node --version` inside the pane, not just in
your login shell (`exec bash` in the pane, or `./run-tmux.sh stop && ./run-tmux.sh`, is the
quickest way to re-read it). And the **API is unaffected** either way: it is Python, so it keeps serving
:8555 while the UI pane is broken, which is why the left pane looks healthy here.

### UI: `Cannot find module '../rolldown-binding.linux-x64-gnu.node'`

The sequel to the version failure above, and the least obvious of the four: Node is now new
enough, the packages are all present, and `ng serve` still dies — thirty
`node:internal/modules/cjs/loader` frames pointing at a file inside
`node_modules/vite/node_modules/rolldown/dist/shared/`.

Angular 22 serves through Vite, Vite 8 bundles with **rolldown**, and rolldown is a Rust binary
shipped as one compiled npm package per platform (`@rolldown/binding-linux-x64-gnu` and fourteen
others), each declared as an **optional** dependency. npm silently skips an optional dependency
whose `engines` do not match the Node running the install — and every one of those packages needs
`^20.19.0 || >=22.12.0`. So the install done on Node 18 fetched *none* of them; that is what the
`EBADENGINE` wall was warning about.

**Upgrading Node does not fix it.** npm looks at the tree, finds every non-optional dependency in
place, and does nothing. The install has to be redone on the new Node:

```bash
node --version                       # confirm the new one is active in *this* pane first
cd ~/ST-V3/AgentCraft-HexaAgent/frontend
rm -rf node_modules && npm ci        # npm ci replaces the tree; npm install would patch it
npm start
```

`npm install` on its own is not enough here — npm counts a skipped optional dependency as
satisfied, so it changes nothing. The tree has to go.

`npm start` now reports this in plain words before Angular loads
(`frontend/scripts/check-native.cjs`, which names the binding your machine needs), and
`run-tmux.sh` treats a tree with no platform binary the same as a missing one and reinstalls the
pane on the way up. The same message covers a `node_modules` copied from another OS — a Windows
tree carries only `binding-win32-x64-msvc`, which is useless on Linux.

### API: `UnicodeDecodeError: 'utf-8' codec can't decode byte 0x97 in position 34`

The backend pane fills with `importlib._bootstrap` frames ending in `dotenv/parser.py`, and the
API never binds. Nothing is wrong with the code: the `.env` is not UTF-8.

`0x97` is what an em dash (`—`) becomes when a file is saved as cp1252 — which is what a Windows
editor, or a copy-paste through one, does to a file en route to the box. Every reader of a `.env`
decodes strict UTF-8, so a single such byte **in a comment** is fatal, and the traceback names a
codec and a byte offset rather than the file it was reading.

Fixed at both ends now:

- The committed `.env.example` is pure ASCII (`--`, not `—`), so a copy of it cannot carry one of
  these bytes in the first place.
- `backend/app/core/config.py` checks the bytes at import, and if the file is not UTF-8 it
  re-encodes it in place, keeping the original as `.env.cp1252-backup`, and says so:

  ```
  [env] /home/ubuntu/.../.env is not UTF-8 (byte 0x97 at position 34); read as cp1252.
  [env] rewrote it as UTF-8; original kept at .env.cp1252-backup
  ```

  It repairs rather than tolerates because tolerating cannot work: `litellm` calls `load_dotenv()`
  itself with no encoding to pass, so importing the LLM client re-reads the file strictly however
  carefully this app reads it. Values are unaffected — keys and values are ASCII, so only the
  comment punctuation changes.

So `git pull && ./run-tmux.sh` is the whole fix. By hand, if you prefer:

```bash
cd ~/ST-V3/AgentCraft-HexaAgent
iconv -f cp1252 -t utf-8 .env -o /tmp/env.utf8 && mv /tmp/env.utf8 .env
file .env            # expect: ASCII text, or UTF-8 Unicode text
```

### API: `could not translate host name "…rds.amazonaws.com" … Name or service not known`

`DATABASE_URL` points at a Postgres host this machine cannot resolve, so the very first
connection fails and uvicorn exits with `Application startup failed`. **"Name or service not
known" is DNS**: the endpoint is wrong, deleted, or private to a VPC this box is not in — not
a security-group problem, which would time out or refuse instead.

The usual cause is an endpoint left in `.env` from another project on the same box. This repo's
default needs no server at all:

```bash
cd ~/ST-V3/AgentCraft-HexaAgent
grep -n DATABASE_URL .env                       # what is it actually pointing at?
sed -i 's#^DATABASE_URL=.*#DATABASE_URL=sqlite:///./agentcraft.db#' .env
./run-tmux.sh stop && ./run-tmux.sh
```

Nothing else in the app changes with the database — SQLite is what `.env.example` ships. If you
do want that Postgres, confirm the endpoint resolves first (`getent hosts <host>`), then that
the port is open (`nc -vz <host> 5432`). Note that **SQLite and Postgres do not share data**:
switching back gives you the local file's contents, not the RDS instance's.

`init_db()` now reports this itself — host, port and database, the driver's one-line reason,
and the sqlite fallback, with the password stripped — instead of a SQLAlchemy traceback. And
`run-tmux.sh` warns before starting anything if the host does not resolve.

---

## When a pane says `Terminated`

Not a startup failure — the opposite. Everything came up cleanly, the UI built and served,
and then some time later the pane looked like this while the backend beside it carried on
answering health checks:

```
Application bundle generation complete. [79.673 seconds]
Watch mode enabled. Watching for file changes...
Terminated
ubuntu@ip-10-92-27-51:~/projects/AgentCraft-HexaAgent/frontend$
```

**`Terminated` is the shell reporting SIGTERM — signal 15, sent from outside the process.** It
is not a crash, not a build error and not the app exiting on its own; something on the machine
asked it to stop. That one word was the entire diagnosis available, and because the pane then
ran `exec bash`, even that scrolled away behind a prompt while the UI stayed down.

**What is different now.** Both panes run under `deploy/supervise.sh`, which restarts the
service and, on every exit, says what happened:

```
[supervise ui] 2026-09-07 14:54:24 — killed by SIGTERM (signal 15) after 1841s up.
  memory : 1966 MB total, 88 MB available · no swap
[supervise ui] this supervisor was signalled as well (SIGTERM), so the whole process group was
[supervise ui]   targeted — not just the service. Nothing inside the app can do that.
[supervise ui] the system logged a memory kill in the same window:
         earlyoom[612]: sending SIGTERM to process 3241 "node" uid 1000
[supervise ui] so this was almost certainly memory pressure, not a fault in the app.
[supervise ui] restarting in 3s.  (Ctrl-c to stop for good.)
```

Signal by name, how long it had been up, the memory at that moment, and any out-of-memory
record from the same window.

**The kill hits the whole process group.** This is worth knowing because it is counter-intuitive:
the reaper does not pick off the one big `node` process, it takes everything in that pane's
process group, the supervisor included. (The backend pane is a *different* process group, which
is exactly why it sits there answering health checks throughout.) So a signal arriving at the
supervisor is **not** evidence that anyone asked for a stop — an early version of this script
assumed it was, and reported `stopped on request after 572s — not restarting` when nothing had
requested anything. What separates intent from a reaper is which signal arrives:

| Signal | Sent by | Result |
|---|---|---|
| SIGINT | `Ctrl-c` in the pane | stops for good |
| SIGHUP | `./run-tmux.sh stop`, `tmux kill-session` — the pane closes and the pty hangs up | stops for good |
| **SIGTERM** | `earlyoom`, `systemd-oomd`, agent-style memory policing | **restarts** |
| SIGKILL | the kernel's own OOM killer (the pane shows `Killed`, not `Terminated`) | restarts |

One consequence to know about: **`kill <pid>` no longer stops a pane**, because its default
signal is SIGTERM and that now means "something reaped this". Use `Ctrl-c` or
`./run-tmux.sh stop`. The supervisor names the signal in either case, so the log always says
which of the two happened.

**The usual cause is memory.** The UI dev server holds the whole bundle plus a file watcher in
one Node heap, which makes it comfortably the largest process on the box and therefore the first
thing any memory reaper picks.

**The build time is the same evidence read another way.** This app's initial bundle is about
16 kB. If the build line reports anything like these, the machine is short of memory, and the
reaping is a symptom rather than the problem:

```
Application bundle generation complete. [ 79.673 seconds]     # already slow
Application bundle generation complete. [484.420 seconds]     # eight minutes — badly starved
```

`run-tmux.sh` warns about this before starting anything when the box has under 2 GB of RAM and
no swap. And after three memory kills the supervisor stops repeating the swap advice and says
plainly that the box cannot host a dev server — it still restarts, because the service is wanted
up, but an eight-minute build that keeps being reaped is not something restarting can fix.

**Confirm it, then fix it.** To check by hand:

```bash
free -h                                                        # RAM and swap
sudo journalctl --since '-30 min' | grep -i -e oom -e killed    # who sent it
systemctl is-enabled agentcraft-ui 2>/dev/null                  # a systemd copy competing for the port?
```

Two durable fixes, in order of preference:

```bash
# 1. Stop running a *dev* server on the box. Build once, serve the static files.
cd ~/ST-V3/AgentCraft-HexaAgent/frontend && npm run build
#    then serve frontend/dist/ from nginx — no watcher, a fraction of the memory.

# 2. Or give the machine swap, so a spike does not have to come out of a running process.
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile \
  && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab      # survive a reboot
```

**If the pane gives up instead of restarting**, it will say so: four starts in a row that each
lasted under 25 seconds is a failure to *start*, not something killing a healthy process, so it
stops and leaves the real error on screen rather than burying it under more attempts. That case
belongs to [Startup failures](#startup-failures-that-look-worse-than-they-are) above.

And if the port is still held when the service exits, the supervisor waits for it to clear and
names the holder rather than restarting into `Address already in use` — see
[Freeing a stuck port](#freeing-a-stuck-port). It never kills anything by name or pattern; on a
box with other projects on it, a `pkill -f node` is how you take out someone else's work.

---

## Freeing a stuck port

Killing a tmux session normally takes its processes with it. When a port stays busy anyway —
`[Errno 98] Address already in use` — find and stop the holder:

```bash
sudo lsof -i :8555            # backend
sudo lsof -i :4225            # UI
kill -9 <PID>
```

Uvicorn's `--reload` runs a worker child, so if the parent is gone but the port is held,
check for a leftover child:

```bash
ps -ef | grep -e uvicorn -e "ng serve" | grep -v grep
```

---

## Open the app

| What | From the box itself | From anywhere else |
| --- | --- | --- |
| UI | `http://localhost:4225` | `http://<public-ip>:4225` — e.g. `http://44.194.125.199:4225` |
| API docs (Swagger) | `http://localhost:8555/docs` | `http://<public-ip>:8555/docs` |

Behind the reverse proxy the same two panes are also reachable as
**`https://agentcraft-poc.bpsgentech.com`** (UI) and **`https://agent-craft-be.bpsgentech.com`**
(API, `/docs` included). No extra process and no second start: the ALB's host-based routing sends
each hostname to a target group pointing at 4225 or 8555 on this box. The pairing is declared once
in `.env` / `.env.example` —

```env
FRONTEND_URL=https://agentcraft-poc.bpsgentech.com
BACKEND_URL=https://agent-craft-be.bpsgentech.com
```

— because a UI served on 443 cannot tell from its own URL that the API is on a *different*
domain; without those lines every call goes to `https://agentcraft-poc…/api/v1/…` and 404s. They
also put the UI's origin in the API's CORS allowlist and give the CLI the API's public address.
The Angular half applies only to a browser already on that host, so the three URLs in the table
above keep behaving exactly as they always did. See [README → Behind a reverse
proxy](README.md#behind-a-reverse-proxy-with-the-ui-and-api-on-different-domains).

Check both from the box after `./run-tmux.sh`:

```bash
curl -s https://agent-craft-be.bpsgentech.com/health   # {"status":"ok","service":"agentcraft"}
curl -s -o /dev/null -w '%{http_code}\n' https://agentcraft-poc.bpsgentech.com/   # 200
```

Log in with the `SUPER_ADMIN_EMAIL` / `SUPER_ADMIN_PASSWORD` from `.env`. Swagger's
**Authorize** button takes the same credentials — email in the `username` field.

Swagger's page is built at startup, so after a `git pull` that changes endpoints or their
descriptions you must **restart the API** to see it; reloading the browser is not enough.

**On EC2 the security group must allow inbound TCP on both `4225` and `8555`.** The browser
calls the API directly, so opening only the UI port loads the page and then fails every API
call — which looks like a broken app rather than a firewall rule.

Confirm the backend is reachable before blaming the UI:

```bash
curl http://localhost:8555/health                # on the box
curl http://44.194.125.199:8555/health           # from your laptop
# {"status":"ok","service":"agentcraft"}
```

> `npm start` runs Angular's **development** server — fine for a demo or an internal box, not
> for production traffic. For that, `npm run build` and serve `frontend/dist/` from nginx.

---

## Use the CLI on the box

Same commands as locally — the CLI finds the API on loopback, so nothing to configure:

```bash
cd ~/ST-V3/AgentCraft-HexaAgent/cli
source .venv/bin/activate
agentcraft health          # prints the URL it resolved
agentcraft auth login --email admin@ac.com --password '...'
agentcraft sessions
```

`Connection refused` here means the **backend is not running**, not that the URL is wrong —
attach to the `api` session (or pane) and look at the log. Do not set `PUBLIC_HOST` or
`AGENTCRAFT_API_URL` to the public IP to fix it: an EC2 instance cannot connect to its own
elastic IP, so that makes it worse. See the README's *Running the CLI on EC2*.

Quick check of what is running:

```bash
tmux ls                                  # is there an api session at all?
curl http://127.0.0.1:8555/health        # is the backend answering?
```

An empty `tmux ls` (or one listing only unrelated sessions) is the answer: nothing is
serving the API, so start it with any option above.

---

## Keeping it up without tmux (systemd)

A tmux session belongs to the shell that made it. It survives an SSH disconnect, but **not**
a reboot, and a process that crashes inside it stays dead. If the backend keeps being "not
running" when you come back, let systemd own it instead:

```bash
cd ~/ST-V3/AgentCraft-HexaAgent
sudo ./deploy/install-service.sh          # both services; or  api  /  ui
```

Restarts on crash, starts at boot, runs as the user that owns the checkout.

```bash
sudo systemctl status agentcraft-api
sudo journalctl -u agentcraft-api -f      # logs, like watching the tmux pane
sudo systemctl restart agentcraft-api
sudo systemctl stop agentcraft-api        # stays stopped until you start it
sudo systemctl disable --now agentcraft-api agentcraft-ui    # uninstall
```

> Use **one or the other**, not both — two copies fighting over port 8555 means the second
> fails with `Address already in use`. Stop the tmux session before installing the service.
> tmux is still nicer while you are actively working: logs in front of you, `Ctrl-c` to
> restart.
