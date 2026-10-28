"""AgentCraft Typer CLI — talks to FastAPI as single source of truth."""

from __future__ import annotations

import json
import os
import re
import struct
import sys
import time
from pathlib import Path
from typing import Optional

import httpx
import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

# Avoid Windows cp1252 crashes on Rich/Typer unicode (em dash, arrows, …).
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

app = typer.Typer(
    help="AgentCraft - generate IDE agents, skills & rules for Claude Code, Cursor, Windsurf & GitHub Copilot",
    no_args_is_help=True,
)
docs_app = typer.Typer(help="Path A document commands")
interview_app = typer.Typer(help="Path B interview commands")
platform_app = typer.Typer(help="IDE platform commands")
diagrams_app = typer.Typer(
    help="Process blueprint: SIPOC, process flow & swimlane PNGs, follow-ups, freeze"
)
plan_app = typer.Typer(help="Review / edit plan")
config_app = typer.Typer(help="LLM configuration")
auth_app = typer.Typer(help="Signup / login / logout")
admin_app = typer.Typer(
    help=(
        "Super admin: approve / reject / delete / restore users, inspect sessions; "
        "also use init/wizard for your own projects"
    )
)

app.add_typer(auth_app, name="auth")
app.add_typer(admin_app, name="admin")
app.add_typer(docs_app, name="docs")
app.add_typer(interview_app, name="interview")
app.add_typer(platform_app, name="platform")
app.add_typer(diagrams_app, name="diagrams")
app.add_typer(plan_app, name="plan")
app.add_typer(config_app, name="config")

console = Console()
#: Uploads the API can extract text from — mirrors ALLOWED_EXTENSIONS in
#: backend/app/services/parser/documents.py. Checked here first so a typo in a path costs a
#: message instead of an upload and a 400, and every sheet of an .xlsx/.xls is read server-side.
ALLOWED_DOC_EXTS = {
    ".pdf",
    ".docx",
    ".md",
    ".markdown",
    ".txt",
    ".csv",
    ".tsv",
    ".xlsx",
    ".xlsm",
    ".xls",
    ".png",
    ".jpg",
    ".jpeg",
}
ALLOWED_DOC_LABEL = "png/jpeg/pdf/docx/md/txt/csv/tsv/xlsx/xls"
#: Sentinel sent when an optional interview question is skipped (matches backend).
SKIP_ANSWER = "—"
STATE_DIR = Path.home() / ".agentcraft"
CURRENT_FILE = STATE_DIR / "current"
TOKEN_FILE = STATE_DIR / "token"
#: WORKBREAKDOWN.md / SDD.md poll — 4s interval matches the UI's wbsPoll; the cap keeps a
#: terminal command from hanging forever when Bedrock is slow or unavailable.
WBS_POLL_SECONDS = 4
WBS_POLL_ATTEMPTS = 45


#: Fallback API port, kept in step with backend/app/core/config.py and .env.example.
DEFAULT_API_PORT = "8555"


def _env_from_dotenv() -> dict[str, str]:
    """Read the repo-root .env, so the CLI targets the same port as the API and UI.

    The CLI has no dependency on python-dotenv (it ships with only httpx/typer/rich, and
    adding one would force everyone to reinstall), so this parses the handful of keys it
    needs itself. Values already in the real environment always win — see `api_url`.

    Searched relative to this file *and* upwards from the working directory. The first
    covers `pip install -e .` from the repo; the second covers a non-editable install,
    where the package sits in site-packages and parents[2] is not the repo at all — the
    reason a CLI installed one way on a laptop and another way on a server picked up
    different ports.
    """
    values: dict[str, str] = {}
    for root in _dotenv_roots():
        # .env.example is read first so a fresh clone with no .env still gets the documented
        # ports; a real .env overrides it key by key.
        for name in (".env.example", ".env"):
            path = root / name
            if not path.exists():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            for raw in text.splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                value = value.strip().strip('"').strip("'")
                if value:
                    values[key.strip()] = value
        if values:
            # Nearest repo root wins outright; do not blend keys across two checkouts.
            break
    return values


def _dotenv_roots() -> list[Path]:
    """Directories that might hold the repo-root .env, nearest first.

    Marked by `.env.example`, which is committed and sits only at the repo root — unlike
    `.env`, which is gitignored and may not exist yet on a fresh clone.
    """
    roots: list[Path] = []
    here = Path(__file__).resolve()
    # cwd first: running inside a checkout should use that checkout's .env, even if the
    # `agentcraft` on PATH was installed from a different copy of the repo.
    for start in (Path.cwd().resolve(), here.parent):
        for candidate in (start, *start.parents):
            if candidate not in roots and (candidate / ".env.example").exists():
                roots.append(candidate)
    # Fallback for an editable install: cli/agentcraft/main.py -> repo root.
    if len(here.parents) >= 3 and here.parents[2] not in roots:
        roots.append(here.parents[2])
    return roots


def api_url_candidates() -> list[str]:
    """API base URLs to try, best first.

    AGENTCRAFT_API_URL (environment, then repo-root .env) pins the answer to exactly one
    URL — that is the escape hatch for an API behind a proxy, on https, or on another host.

    Otherwise the CLI runs on the same machine as the API, so **loopback comes first**, and
    BACKEND_URL / PUBLIC_HOST are only fallbacks. That order matters on EC2: PUBLIC_HOST holds the public
    IP for the browser's benefit, but an instance cannot reach its own public address —
    traffic to the elastic IP leaves for the internet gateway and is not routed back in. A
    CLI that preferred PUBLIC_HOST therefore got "Connection refused" against a backend
    running perfectly well on that same box.

    Both loopback spellings are listed because they are not interchangeable: something
    bound to IPv6 answers on ::1 but not 127.0.0.1, and vice versa.
    """
    dotenv = _env_from_dotenv()
    explicit = os.getenv("AGENTCRAFT_API_URL") or dotenv.get("AGENTCRAFT_API_URL")
    if explicit:
        return [explicit.rstrip("/")]

    port = os.getenv("API_PORT") or dotenv.get("API_PORT") or DEFAULT_API_PORT
    urls = [f"http://127.0.0.1:{port}", f"http://localhost:{port}"]
    # BACKEND_URL is the deployed API behind its proxy — the same address the browser uses. It is
    # a *candidate*, not a pin: on the box itself loopback is both faster and immune to the proxy,
    # DNS and TLS, so it stays first, and this is what makes the CLI work from a laptop with no
    # extra configuration. AGENTCRAFT_API_URL above is still the way to force one exact URL.
    backend_url = (os.getenv("BACKEND_URL") or dotenv.get("BACKEND_URL") or "").strip()
    if backend_url:
        urls.append(backend_url.rstrip("/"))
    public_host = (os.getenv("PUBLIC_HOST") or dotenv.get("PUBLIC_HOST") or "").strip()
    if public_host and public_host not in ("127.0.0.1", "localhost", "0.0.0.0"):
        urls.append(f"http://{public_host}:{port}")
    return urls


#: Resolved URL, cached for the life of the process — one probe per command, not per call.
_resolved_api_url: str | None = None


def api_url() -> str:
    """Base URL of the API — the first candidate that answers /health.

    A single pinned URL is returned without probing. With several candidates the probe is
    a ~1s /health request per URL and stops at the first success, so the normal local case
    costs a millisecond or two against loopback.

    When nothing answers, the first candidate is returned anyway: the request then fails
    against the URL a person would expect, which is what makes the error message useful.
    """
    global _resolved_api_url
    if _resolved_api_url is not None:
        return _resolved_api_url

    candidates = api_url_candidates()
    resolved = candidates[0]
    if len(candidates) > 1:
        for url in candidates:
            try:
                if httpx.get(f"{url}/health", timeout=1.5).is_success:
                    resolved = url
                    break
            except httpx.HTTPError:
                continue
    _resolved_api_url = resolved
    return resolved


def save_token(token: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(token.strip(), encoding="utf-8")


def load_token() -> str | None:
    if TOKEN_FILE.exists():
        t = TOKEN_FILE.read_text(encoding="utf-8").strip()
        return t or None
    return None


def clear_token() -> None:
    if TOKEN_FILE.exists():
        TOKEN_FILE.unlink()


def auth_headers() -> dict[str, str]:
    token = load_token()
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


class _ApiClient(httpx.Client):
    """httpx client that turns "the API is not there" into a readable message.

    Without this, a backend that is down or on another port surfaces as a 40-frame httpx
    traceback ending in `ConnectError: [Errno 111] Connection refused` — which says nothing
    about *which* URL was tried, and on a server reads as a bug in the CLI rather than a
    backend that is not running. Wrapping `request` catches every command at once, since
    they all go through this client.
    """

    def request(self, *args, **kwargs):  # type: ignore[override]
        try:
            return super().request(*args, **kwargs)
        except httpx.ConnectError:
            _fail_unreachable("Cannot reach the AgentCraft API")
        except httpx.ConnectTimeout:
            _fail_unreachable("Timed out connecting to the AgentCraft API")
        except httpx.ReadTimeout:
            # Not a connectivity problem: the API accepted the request and is still
            # working. Usually a long Bedrock generation, so say so instead of sending
            # someone off to check ports and firewalls.
            console.print(
                f"[red]The API did not respond in time[/red] ({api_url()}).\n"
                "[dim]Generation can take a few minutes — check the backend log, then "
                "retry. `agentcraft status` shows where the project got to.[/dim]"
            )
            raise typer.Exit(1)


def _is_loopback_url(url: str) -> bool:
    return "//127.0.0.1" in url or "//localhost" in url or "//[::1]" in url


def _repo_root() -> Path | None:
    """Repo root, if the CLI is being run from or beside a checkout."""
    roots = _dotenv_roots()
    return roots[0] if roots else None


def _fail_unreachable(headline: str) -> None:
    """Say what is actually wrong and give the one command that fixes it. Never returns.

    A refused connection on *loopback* is not ambiguous: nothing is listening on that port
    on this machine, so the backend is not running. Saying "check 1, 2, 3" there invites
    people to go changing PUBLIC_HOST and AGENTCRAFT_API_URL — which cannot help, and on
    EC2 actively breaks things. So diagnose the loopback case outright and keep the list of
    possibilities for the remote case, where the cause really is unknown from here.
    """
    url = api_url()
    port = url.rsplit(":", 1)[-1]

    if _is_loopback_url(url):
        console.print(f"[red]The AgentCraft backend is not running[/red] on {url}")
        console.print(
            f"[dim]Nothing is listening on port {port} on this machine — the CLI needs the "
            "API for every command.[/dim]"
        )
        console.print("\n[bold]Start it:[/bold]")
        root = _repo_root()
        # os.sep, not a hardcoded slash: the path is copy-pasted, and a mixed
        # C:\path/backend reads like a bug in the tool.
        backend = str(root / "backend") if root else f"<repo>{os.sep}backend"
        if os.name == "nt":
            console.print(
                f'  [bold]cd "{backend}"; .\\.venv\\Scripts\\Activate.ps1; '
                f".\\run_api.ps1[/bold]"
            )
        else:
            console.print(
                f'  [bold]cd "{backend}" && source .venv/bin/activate && ./run_api.sh[/bold]'
            )
        if os.name != "nt":
            # tmux is the answer to "it died when I closed SSH", which is the usual way
            # this happens on a server. Irrelevant noise on Windows.
            console.print(
                "\n[dim]Keep it running after you log out (EC2): "
                "[bold]./run-tmux.sh[/bold] — see TMUX.md[/dim]"
            )
        console.print(f"[dim]Then check: [bold]curl {url}/health[/bold][/dim]")
        # Only relevant if they meant to reach a *different* machine; last, and quiet.
        console.print(
            "\n[dim]Is the API on another machine? "
            f"[bold]export AGENTCRAFT_API_URL=http://<api-host>:{port}[/bold][/dim]"
        )
        raise typer.Exit(1)

    # Not loopback: an explicitly configured or PUBLIC_HOST address, so the API could be
    # down, firewalled, or simply somewhere else. All three are worth naming.
    console.print(f"[red]{headline}[/red] at [bold]{url}[/bold]")
    console.print()
    console.print("[bold]Check, in order:[/bold]")
    console.print(f"  1. The API is up on that host — [bold]curl {url}/health[/bold]")
    console.print(
        f"  2. Port {port} is reachable — on EC2 the security group must allow inbound "
        f"TCP {port} from your address"
    )
    console.print(
        f"  3. The URL is right — [bold]AGENTCRAFT_API_URL[/bold] / "
        f"[bold]PUBLIC_HOST[/bold] in .env point here"
    )
    candidates = api_url_candidates()
    if len(candidates) > 1:
        console.print(f"     [dim]Tried: {', '.join(candidates)}[/dim]")
    console.print(
        "\n[dim]Running on the API's own box? Use loopback, not the public IP — an EC2 "
        "instance cannot connect to its own elastic IP.[/dim]"
    )
    raise typer.Exit(1)


def client() -> httpx.Client:
    return _ApiClient(base_url=api_url(), timeout=300.0, headers=auth_headers())


def require_auth() -> None:
    if load_token():
        return
    console.print("[red]Not logged in.[/red] Run: [bold]agentcraft auth login[/bold] or [bold]agentcraft auth signup[/bold]")
    raise typer.Exit(1)


def save_current(project_id: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    CURRENT_FILE.write_text(project_id, encoding="utf-8")


def current_id(explicit: Optional[str] = None) -> str:
    if explicit:
        return explicit
    if CURRENT_FILE.exists():
        return CURRENT_FILE.read_text(encoding="utf-8").strip()
    console.print("[red]No active project. Run: agentcraft init[/red]")
    raise typer.Exit(1)


def show_status(data: dict) -> None:
    pid = str(data.get("id") or "")
    table = Table(title="Project status", show_header=True, expand=True)
    table.add_column("Field", style="bold", no_wrap=True)
    table.add_column("Value", overflow="fold", no_wrap=False)
    table.add_row("Project ID", pid or "-")
    table.add_row("Name", data.get("name", "") or "-")
    table.add_row("State", data.get("state", "") or "-")
    table.add_row("Path", str(data.get("path") or "-"))
    table.add_row("Platform", str(data.get("platform") or "-"))
    docs = ((data.get("brief") or {}).get("documents")) or []
    if docs:
        table.add_row("Documents", _documents_line(docs))
    if data.get("has_diagrams"):
        frozen = "approved" if data.get("diagrams_frozen") else "draft — generation is blocked"
        table.add_row("Blueprint", f"v{data.get('diagram_version') or 1} ({frozen})")
    blockers = data.get("blockers") or []
    table.add_row("Next", "; ".join(blockers) if blockers else "-")
    if data.get("error"):
        table.add_row("Error", str(data["error"]))
    console.print(table)
    if pid:
        console.print(f"[dim]Use -p {pid} on other commands[/dim]")


def _human_bytes(size: object) -> str:
    """Match the UI's size formatting. Empty for 0 (pre-upgrade uploads)."""
    try:
        n = int(size or 0)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _documents_line(docs: list) -> str:
    """One-line document summary, same names the UI shows."""
    parts = []
    for d in docs:
        name = str((d or {}).get("filename") or "").strip()
        if not name:
            continue
        size = _human_bytes((d or {}).get("size_bytes"))
        parts.append(f"{name} ({size})" if size else name)
    return ", ".join(parts) if parts else "-"


def _full_id(value: object) -> str:
    """Always return the complete UUID/id string (never truncated)."""
    text = str(value or "").strip()
    return text if text else "-"


def _find_admin_user(c: httpx.Client, user_id: str) -> dict:
    """
    Look up one account (live or deleted) so a destructive command can preview it.

    Exits with a pointer to `admin users` instead of a bare 404, since the id has to be
    copied by hand. The listing includes deleted accounts, so purge/restore resolve too.
    """
    r = c.get("/api/v1/admin/users")
    _raise_for_status(r)
    target = next((u for u in r.json() if _full_id(u.get("id")) == user_id.strip()), None)
    if target is None:
        console.print(
            f"[red]No user with id {user_id}.[/red] "
            "List ids with: [bold]agentcraft admin users[/bold]"
        )
        raise typer.Exit(1)
    return target


_IDE_PREFIXES = (
    ".cursor/",
    ".claude/",
    ".windsurf/",
    ".agents/",
    ".github/agents/",
    ".github/skills/",
    ".github/instructions/",
)
# .gitignore and CLAUDE.md are generated by the exporter from the plan, not scaffolded, so
# they are workspace files and never source files — same as README.md.
_IDE_FILES = {
    "README.md",
    "README.agentcraft.md",
    "AGENTS.md",
    ".mcp.json",
    ".gitignore",
    "CLAUDE.md",
    ".github/copilot-instructions.md",
}


def _is_ide_or_readme(path: str) -> bool:
    """Mirror of the backend renderer's check — keeps the CLI's counts identical to the UI's."""
    p = (path or "").replace("\\", "/").lstrip("/")
    return p in _IDE_FILES or p.startswith(_IDE_PREFIXES)


def _source_file_count(plan: dict) -> int:
    """
    App scaffold files only — the "Source files" figure, same as the UI's Files tab.

    Always smaller than the workspace path count, which also includes the IDE folder,
    README.md, WORKBREAKDOWN.md and SDD.md.
    """
    return sum(
        1
        for f in (plan.get("source_tree") or [])
        if isinstance(f, dict)
        and str(f.get("path") or "").strip()
        and not _is_ide_or_readme(str(f.get("path")))
    )


def _ensure_document(
    c: httpx.Client,
    pid: str,
    plan: dict,
    *,
    filename: str,
    endpoint: str,
    complete_key: str,
    marker: str,
) -> dict:
    """
    Generate one of the two long-form documents if the project has not got it yet.

    The UI does this automatically when the Review/Export step loads
    (`maybeGenerateWorkspaceDocuments` in wizard.component.ts), so a project exported from
    the CLI used to ship the short fallback documents while the same project opened in the
    browser got the full Bedrock ones. Same trigger conditions and the same endpoints here,
    so both front ends produce the same files.

    Returns the refreshed plan. Never raises: a missing or failed document must not lose
    the user their export, exactly as in the UI, where the failure is a banner rather than
    a dead end.
    """
    if plan.get(complete_key):
        return plan
    try:
        r = c.post(endpoint, json={})
        if not r.is_success:
            return plan
        # The API refuses to start one document while the other is in flight — both write
        # the same plan row. It says so by handing back a `progress` that names the other
        # document, and there is nothing to poll for in that case.
        queued = r.json() or {}
        progress = queued.get("progress") or ""
        if progress and marker not in progress:
            console.print(f"[yellow]{filename}: another document is generating — skipped.[/yellow]")
            return queued.get("plan") or plan
        console.print(f"[dim]{filename}: generating with Bedrock…[/dim]")
        # Backend runs it in a background thread and reports through `progress`, so poll
        # the project like the UI's 4s wbsPoll. Capped rather than unbounded: a terminal
        # command has to return, and the export below still works with the fallback.
        for _ in range(WBS_POLL_ATTEMPTS):
            time.sleep(WBS_POLL_SECONDS)
            pr = c.get(f"/api/v1/projects/{pid}")
            if not pr.is_success:
                break
            data = pr.json()
            if marker in (data.get("progress") or ""):
                continue
            fresh = data.get("plan") or plan
            if fresh.get(complete_key):
                console.print(f"[green]Detailed {filename} ready.[/green]")
            return fresh
        console.print(
            f"[yellow]{filename} still generating — exporting the current version.[/yellow]"
        )
    except httpx.HTTPError:
        pass
    return plan


def _ensure_work_breakdown(c: httpx.Client, pid: str, plan: dict) -> dict:
    """Backfill the detailed WORKBREAKDOWN.md. See `_ensure_document`."""
    return _ensure_document(
        c,
        pid,
        plan,
        filename="WORKBREAKDOWN.md",
        endpoint=f"/api/v1/projects/{pid}/plan/work-breakdown/generate",
        complete_key="work_breakdown_complete",
        marker="WORKBREAKDOWN",
    )


def _ensure_solution_design(c: httpx.Client, pid: str, plan: dict) -> dict:
    """Backfill the detailed SDD.md solution design document. See `_ensure_document`."""
    return _ensure_document(
        c,
        pid,
        plan,
        filename="SDD.md",
        endpoint=f"/api/v1/projects/{pid}/plan/solution-design/generate",
        complete_key="solution_design_complete",
        marker="SDD",
    )


def _ensure_generated_docs(c: httpx.Client, pid: str, plan: dict) -> dict:
    """
    Backfill both long-form documents, work breakdown first.

    Sequential, not concurrent: the API refuses to start the second while the first is in
    flight because both write the same plan row, and the loser's `file_overrides` would
    drop the winner's document.
    """
    plan = _ensure_work_breakdown(c, pid, plan)
    return _ensure_solution_design(c, pid, plan)


def _print_generated_root_files(paths: list[str]) -> None:
    """
    Note the root files the exporter generates, so the CLI explains them like the UI does.

    They are not in `source_tree` — they are rendered from the plan on every export, which
    is why existing projects get them with no migration.
    """
    norm = {(p or "").replace("\\", "/").lstrip("/") for p in paths}
    notes = []
    if ".gitignore" in norm:
        notes.append("[bold].gitignore[/bold] (tech-stack rules; AI workspace stays tracked)")
    if "CLAUDE.md" in norm:
        notes.append("[bold]CLAUDE.md[/bold] (project memory Claude Code loads each session)")
    if notes:
        console.print("[dim]Generated at root:[/dim] " + "  ·  ".join(notes))

    # The two picture folders, named for the same reason: they hold PNGs, so `preview -f`
    # cannot show them, and without a line here they look like empty folders in the tree.
    for prefix, label in (
        ("docs/diagrams/", "the approved process blueprint"),
        ("docs/architecture/", "the three architectural views SDD.md §3.2 embeds"),
    ):
        pngs = sorted(p for p in norm if p.startswith(prefix) and p.endswith(".png"))
        if pngs:
            console.print(
                f"[dim]Diagrams:[/dim] [bold]{prefix}[/bold] ({len(pngs)} PNG — {label})  "
                + "  ·  ".join(p[len(prefix) :] for p in pngs)
            )

    # Rule folder and extension differ per IDE, so name the one that was actually written
    # rather than a generic "rules" — the same detail the UI's Rules tab shows.
    for prefix, label in (
        (".claude/rules/", "plain .md, imported by CLAUDE.md"),
        (".cursor/rules/", ".mdc with alwaysApply/globs"),
        (".windsurf/rules/", ".md with a trigger"),
        (".github/instructions/", ".instructions.md with applyTo"),
    ):
        rule_files = sorted(p for p in norm if p.startswith(prefix))
        if rule_files:
            console.print(
                f"[dim]Rules:[/dim] [bold]{prefix}[/bold] ({len(rule_files)} — {label})  "
                + "  ·  ".join(p[len(prefix) :] for p in rule_files)
            )
            break


def _print_plan_summary(plan: dict, platform: str | None = None) -> None:
    """Match UI review summary: agents/skills/rules + modular source_tree."""
    agents = plan.get("agents") or []
    skills = plan.get("skills") or []
    rules = plan.get("rules") or []
    tree = plan.get("source_tree") or []
    msg = (
        f"[green]Agents:[/green] {len(agents)}  "
        f"[green]Skills:[/green] {len(skills)}  "
        # Every platform exports rules now, Claude included — same count the UI's Rules
        # tab shows. Claude's land in .claude/rules/ as plain .md.
        f"[green]Rules:[/green] {len(rules)}"
    )
    msg += f"  [green]Source files:[/green] {_source_file_count(plan)}"
    console.print(msg)
    if tree:
        paths = [str(f.get("path") or "") for f in tree if isinstance(f, dict)]
        backend_n = sum(1 for p in paths if p.startswith("backend/"))
        frontend_n = sum(1 for p in paths if p.startswith("frontend/"))
        console.print(
            f"[dim]Scaffold:[/dim] main.py"
            + (f" · backend/ ({backend_n})" if backend_n else "")
            + (f" · frontend/ ({frontend_n})" if frontend_n else "")
            + "  → [bold]agentcraft plan tree[/bold] / [bold]agentcraft preview[/bold]"
        )


def _ascii_tree_lines(paths: list[str], *, max_depth: int | None = None) -> list[str]:
    """
    Render paths as a tree, optionally stopping at `max_depth` levels.

    `max_depth` mirrors the UI's collapsed folders: a folder deeper than the limit is
    printed with a count of what it holds instead of its contents, so a 139-file
    workspace fits on screen. None prints everything (the UI's "Expand all").
    """
    root: dict = {}
    for raw in sorted(set(p.replace("\\", "/").lstrip("/") for p in paths if p)):
        node = root
        parts = raw.split("/")
        for i, part in enumerate(parts):
            node = node.setdefault(part, {})
            if i == len(parts) - 1:
                node.setdefault("__file__", True)

    lines: list[str] = []

    def count_files(n: dict) -> int:
        total = 0
        for key, child in n.items():
            if key == "__file__":
                continue
            total += 1 if child.get("__file__") else 0
            total += count_files(child)
        return total

    def walk(n: dict, prefix: str = "", depth: int = 1) -> None:
        entries = sorted(k for k in n if k != "__file__")
        for i, name in enumerate(entries):
            last = i == len(entries) - 1
            branch = "└── " if last else "├── "
            child = n[name]
            is_dir = any(k != "__file__" for k in child)
            suffix = "/" if is_dir and "__file__" not in child else ""
            if is_dir and max_depth is not None and depth >= max_depth:
                held = count_files(child)
                unit = "file" if held == 1 else "files"
                lines.append(f"{prefix}{branch}{name}{suffix} [dim]({held} {unit})[/dim]")
                continue
            lines.append(f"{prefix}{branch}{name}{suffix}")
            if is_dir:
                walk(child, prefix + ("    " if last else "│   "), depth + 1)

    walk(root)
    return lines or ["(empty)"]


def _print_source_tree(plan: dict, *, with_purpose: bool = True) -> None:
    tree = plan.get("source_tree") or []
    if not tree:
        console.print("[yellow]No source_tree on this plan yet. Run generate (or export preview to backfill).[/yellow]")
        return
    paths = [str(f.get("path") or "") for f in tree if isinstance(f, dict)]
    console.print(Panel("\n".join(_ascii_tree_lines(paths)), title="Project structure", expand=False))
    if with_purpose:
        table = Table(title="File purposes", show_header=True, expand=True)
        table.add_column("Path", style="cyan", no_wrap=False)
        table.add_column("Role / purpose", no_wrap=False)
        for f in tree:
            if not isinstance(f, dict):
                continue
            table.add_row(str(f.get("path") or ""), str(f.get("purpose") or ""))
        console.print(table)


def _raise_for_status(r: httpx.Response) -> None:
    if r.is_success:
        return
    if r.status_code == 401:
        console.print(
            "[red]Authentication required.[/red] "
            "Run [bold]agentcraft auth login[/bold] (or signup), then retry."
        )
        raise typer.Exit(1)
    try:
        detail = r.json()
        message = str(detail.get("message") or detail)
    except Exception:
        console.print(f"[red]HTTP {r.status_code}: {r.text}[/red]")
        raise typer.Exit(1)

    console.print(f"[red]{message}[/red]")
    if r.status_code == 403 and "delete" in message.lower():
        # The super admin deleted this account. Drop the stored token so later
        # commands don't all fail the same way (the UI logs out for the same reason).
        # Never on login/signup: the shared client attaches whatever token is on disk,
        # so a deleted person's failed login would otherwise sign out the real admin.
        path = r.request.url.path
        is_auth_attempt = path.endswith("/auth/login") or path.endswith("/auth/signup")
        if not is_auth_attempt and load_token():
            clear_token()
            console.print("[dim]Local token removed (~/.agentcraft/token).[/dim]")
        console.print(
            "[dim]Run [bold]agentcraft auth signup[/bold] to request access again.[/dim]"
        )
    raise typer.Exit(1)


def _store_auth_response(data: dict) -> None:
    token = data.get("access_token")
    if not token:
        console.print("[red]No access_token in response[/red]")
        raise typer.Exit(1)
    save_token(token)
    user = data.get("user") or {}
    console.print(
        f"[green]Signed in[/green] as {user.get('name') or user.get('email')} "
        f"({user.get('email')})"
    )


# ── Auth ─────────────────────────────────────────────────────────────────────


@auth_app.command("signup")
def auth_signup(
    email: Optional[str] = typer.Option(None, "--email", "-e"),
    password: Optional[str] = typer.Option(None, "--password", "-p"),
    confirm_password: Optional[str] = typer.Option(
        None, "--confirm-password", "-c", help="Must match --password"
    ),
    name: Optional[str] = typer.Option(None, "--name", "-n"),
) -> None:
    """Request an account — waits for super admin approval (no JWT yet)."""
    email = (email or Prompt.ask("Email")).strip()
    name = (name or Prompt.ask("Display name", default="AgentCraft User")).strip()
    password = password or Prompt.ask("Password (min 6 chars)", password=True)
    if len(password) < 6:
        console.print("[red]Password must be at least 6 characters[/red]")
        raise typer.Exit(1)
    confirm = confirm_password
    if confirm is None:
        confirm = Prompt.ask("Confirm password", password=True)
    if password != confirm:
        console.print("[red]Password and confirm password do not match[/red]")
        raise typer.Exit(1)
    with client() as c:
        r = c.post(
            "/api/v1/auth/signup",
            json={"email": email, "password": password, "name": name},
        )
        _raise_for_status(r)
        data = r.json()
        console.print(f"[yellow]{data.get('message') or 'Pending admin approval'}[/yellow]")
        console.print(
            "Ask the super admin to approve you, then run: [bold]agentcraft auth login[/bold]"
        )


@auth_app.command("login")
def auth_login(
    email: Optional[str] = typer.Option(None, "--email", "-e"),
    password: Optional[str] = typer.Option(None, "--password", "-p"),
) -> None:
    """Log in and store the JWT locally (account must be approved)."""
    email = (email or Prompt.ask("Email")).strip()
    password = password or Prompt.ask("Password", password=True)
    with client() as c:
        r = c.post("/api/v1/auth/login", json={"email": email, "password": password})
        _raise_for_status(r)
        data = r.json()
        _store_auth_response(data)
        user = data.get("user") or {}
        if user.get("role") == "admin":
            console.print(
                "[cyan]Super admin[/cyan] — manage users: [bold]agentcraft admin users[/bold]\n"
                "Create your own workspace: [bold]agentcraft init[/bold] / [bold]agentcraft wizard[/bold]"
            )


@auth_app.command("logout")
def auth_logout() -> None:
    """Clear the stored JWT."""
    clear_token()
    console.print("[green]Logged out[/green] (token removed from ~/.agentcraft/token)")


@auth_app.command("whoami")
def auth_whoami() -> None:
    """Show the current user (requires login)."""
    require_auth()
    with client() as c:
        r = c.get("/api/v1/auth/me")
        _raise_for_status(r)
        console.print_json(data=r.json())


def _join_names(names: list | None, empty: str = "-") -> str:
    items = [str(n) for n in (names or []) if n]
    return "\n".join(items) if items else empty


def _pipeline_cell(s: dict) -> str:
    """Which context pipeline a session followed, for the admin table.

    The two paths produce very different briefs — uploaded documents versus answers typed
    into the guided interview — and the state name does not say which. A `?` marks a path
    that was *deduced* from what the workspace holds rather than read off the row: sessions
    created before the path was recorded still took one, and calling those "not chosen"
    would be wrong.
    """
    path = str(s.get("path") or "")
    mark = "?" if s.get("path_inferred") else ""
    if path == "docs":
        n = len(s.get("document_names") or [])
        return f"[green]Documents{mark}[/green]\n[dim]{n} file(s)[/dim]"
    if path == "interview":
        n = int(s.get("interview_answer_count") or 0)
        return f"[blue]Interview{mark}[/blue]\n[dim]{n} answer(s)[/dim]"
    return "[yellow]not chosen[/yellow]"


def _blueprint_cell(s: dict) -> str:
    """The blueprint in one cell — version, whether the user approved it, and its size.

    The last follow-up's scope is here too: the version number says how many times the user came
    back, not what they came back for, and a change that rewrote the process moved the generated
    agents with it while a change scoped to one view did not.
    """
    if not s.get("has_diagrams"):
        return "[dim]none[/dim]"
    state = "approved" if s.get("diagrams_frozen") else "draft"
    changes = max(0, int(s.get("diagram_revision_count") or 0) - 1)
    if not changes:
        tail = "never revised"
    else:
        tail = (
            f"{changes} change{'' if changes == 1 else 's'} · "
            f"last: {_scope_label(s.get('diagram_last_scope'))}"
        )
    return (
        f"v{s.get('diagram_version') or 1} ({state})\n"
        f"[dim]{s.get('diagram_step_count') or 0} steps · "
        f"{s.get('diagram_actor_count') or 0} actors[/dim]\n"
        f"[dim]{tail}[/dim]"
    )


def _platform_label(plat: str | None) -> str:
    if not plat:
        return "-"
    if plat == "claude_code":
        return "Claude Code"
    if plat == "cursor":
        return "Cursor"
    if plat == "windsurf":
        return "Windsurf"
    if plat == "github_copilot":
        return "GitHub Copilot"
    return plat


# ── Super admin ───────────────────────────────────────────────────────────────


@admin_app.command("users")
def admin_users(
    status: Optional[str] = typer.Option(
        None, "--status", "-s", help="pending | approved | rejected | deleted"
    ),
    email: Optional[str] = typer.Option(
        None, "--email", "-e", help="Filter one user by email"
    ),
) -> None:
    """
    List users in tables: overview + sessions with agent/skill/rule names (read-only).

    Deleted accounts are listed with status "deleted" — the same archive the Admin UI's
    Deleted tab shows, restorable with: agentcraft admin restore <id>
    """
    require_auth()
    with client() as c:
        r = c.get("/api/v1/admin/users")
        _raise_for_status(r)
        rows = r.json()
        if status:
            status_l = status.strip().lower()
            rows = [u for u in rows if (u.get("status") or "").lower() == status_l]
        if email:
            email_l = email.strip().lower()
            rows = [u for u in rows if (u.get("email") or "").lower() == email_l]
        if not rows:
            console.print("[yellow]No users found for this filter.[/yellow]")
            return

        overview = Table(title="Users overview", show_lines=True, expand=True)
        overview.add_column("Name", style="bold", no_wrap=False, overflow="fold")
        overview.add_column("Email", no_wrap=False, overflow="fold")
        overview.add_column("Role", no_wrap=True)
        overview.add_column("Status", no_wrap=True)
        overview.add_column("Sessions", justify="right", no_wrap=True)
        overview.add_column("User ID (full UUID)", style="cyan", no_wrap=False, overflow="fold")
        pending_for_actions: list[tuple[str, str, str]] = []
        deleted_for_actions: list[tuple[str, str, str, bool]] = []
        for u in rows:
            uid = _full_id(u.get("id"))
            row_status = (u.get("status") or "-").lower()
            is_deleted = row_status == "deleted"
            overview.add_row(
                u.get("name") or "",
                u.get("email") or "",
                u.get("role") or "user",
                # Say what it was, since "deleted" alone hides whether they were approved.
                f"[red]deleted[/red] (was {u.get('previous_status') or '?'})"
                if is_deleted
                else row_status,
                # Their workspaces are held for restore, not live.
                f"{u.get('session_count', 0)} held" if is_deleted else str(u.get("session_count", 0)),
                uid,
            )
            if row_status == "pending" and (u.get("role") or "user") != "admin":
                pending_for_actions.append((u.get("name") or "", u.get("email") or "", uid))
            if is_deleted:
                deleted_for_actions.append(
                    (u.get("name") or "", u.get("email") or "", uid, bool(u.get("restorable", True)))
                )
        console.print(overview)
        console.print("")
        if pending_for_actions:
            console.print("[bold]Approve / reject (copy full UUID):[/bold]")
            for name, em, uid in pending_for_actions:
                console.print(f"  {name} <{em}>")
                console.print(f"    [green]agentcraft admin approve {uid}[/green]")
                console.print(f"    [yellow]agentcraft admin reject {uid}[/yellow]")
            console.print("")
        if deleted_for_actions:
            console.print("[bold]Deleted accounts — restore or erase (copy full UUID):[/bold]")
            for name, em, uid, restorable in deleted_for_actions:
                console.print(f"  {name} <{em}>")
                if restorable:
                    console.print(f"    [green]agentcraft admin restore {uid}[/green]")
                else:
                    console.print(
                        "    [dim]not restorable — deleted before restore existed; "
                        "they must sign up again[/dim]"
                    )
                console.print(f"    [red]agentcraft admin purge {uid}[/red]")
            console.print("")

        for u in rows:
            sessions = u.get("sessions") or []
            title = f"Sessions for {u.get('name')} <{u.get('email')}>  ·  user {_full_id(u.get('id'))}"
            sess_table = Table(title=title, show_lines=True, expand=True)
            sess_table.add_column("Session ID (full UUID)", style="cyan", no_wrap=False, overflow="fold")
            sess_table.add_column("Session", style="bold", no_wrap=False, overflow="fold")
            sess_table.add_column("State", no_wrap=True)
            sess_table.add_column("Platform", no_wrap=True)
            # Same two facts the /admin panel puts on the collapsed session row: which
            # pipeline built the brief, and whether there is a blueprint behind it.
            sess_table.add_column("Pipeline", no_wrap=True)
            sess_table.add_column("Blueprint", no_wrap=True)
            sess_table.add_column("#A/#S/#R", justify="center", no_wrap=True)
            sess_table.add_column("Agents", style="green", no_wrap=False, overflow="fold")
            sess_table.add_column("Skills", style="blue", no_wrap=False, overflow="fold")
            sess_table.add_column("Rules", style="magenta", no_wrap=False, overflow="fold")

            if not sessions:
                sess_table.add_row("(no sessions)", "-", "-", "-", "-", "-", "-", "-", "-", "-")
            else:
                for s in sessions:
                    plat = s.get("platform") or "-"
                    agents = s.get("agent_names") or []
                    skills = s.get("skill_names") or []
                    rules = s.get("rule_names") or []
                    counts = (
                        f"{s.get('agent_count', len(agents))}/"
                        f"{s.get('skill_count', len(skills))}/"
                        f"{s.get('rule_count', len(rules))}"
                    )
                    sess_table.add_row(
                        _full_id(s.get("id")),
                        s.get("name") or "",
                        str(s.get("state") or "-"),
                        _platform_label(plat),
                        _pipeline_cell(s),
                        _blueprint_cell(s),
                        counts,
                        _join_names(agents),
                        _join_names(skills),
                        _join_names(rules),
                    )
            console.print(sess_table)
            console.print("")


@admin_app.command("approve")
def admin_approve(user_id: str = typer.Argument(..., help="Full user UUID from: agentcraft admin users")) -> None:
    """Approve a pending user so they can log in."""
    require_auth()
    with client() as c:
        r = c.post(f"/api/v1/admin/users/{user_id}/approve")
        _raise_for_status(r)
        u = r.json()
        console.print(
            f"[green]Approved[/green] {u.get('email')} ({u.get('status')})  id={_full_id(u.get('id'))}"
        )


@admin_app.command("reject")
def admin_reject(user_id: str = typer.Argument(..., help="Full user UUID from: agentcraft admin users")) -> None:
    """Reject a user (they cannot access the studio)."""
    require_auth()
    with client() as c:
        r = c.post(f"/api/v1/admin/users/{user_id}/reject")
        _raise_for_status(r)
        u = r.json()
        console.print(
            f"[red]Rejected[/red] {u.get('email')} ({u.get('status')})  id={_full_id(u.get('id'))}"
        )


@admin_app.command("delete")
def admin_delete(
    user_id: str = typer.Argument(..., help="Full user UUID from: agentcraft admin users"),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt (for scripts)"
    ),
) -> None:
    """
    Revoke a user's access (same as the Admin UI's Delete user).

    Their workspaces are kept and the account can be brought back with
    `agentcraft admin restore <id>`. Use `agentcraft admin purge <id>` to erase both.
    """
    require_auth()
    with client() as c:
        # Show who is about to go — the UI's confirm modal does the same.
        target = _find_admin_user(c, user_id)
        if (target.get("status") or "") == "deleted":
            console.print(
                f"[yellow]{target.get('email')} is already deleted.[/yellow] "
                "Restore it with [bold]agentcraft admin restore[/bold], or erase it with "
                "[bold]agentcraft admin purge[/bold]."
            )
            raise typer.Exit(1)
        if (target.get("role") or "user") == "admin":
            console.print("[red]The super admin account cannot be deleted.[/red]")
            raise typer.Exit(1)

        sessions = int(target.get("session_count") or 0)
        console.print(
            f"[yellow]Deleting[/yellow] {target.get('name')} <{target.get('email')}> "
            f"· status {target.get('status')} · {sessions} session{'' if sessions == 1 else 's'}"
        )
        if not yes and not Confirm.ask(
            "They lose access immediately; workspaces are held for restore. Continue?",
            default=False,
        ):
            console.print("[dim]Cancelled — nothing was deleted.[/dim]")
            raise typer.Exit(0)

        r = c.request("DELETE", f"/api/v1/admin/users/{user_id}")
        _raise_for_status(r)
        data = r.json()
        console.print(f"[red]Deleted[/red] {data.get('email')}  id={_full_id(data.get('id'))}")
        console.print(f"[dim]{data.get('message') or ''}[/dim]")
        console.print(
            f"[dim]Undo with: [bold]agentcraft admin restore {_full_id(data.get('id'))}[/bold][/dim]"
        )


@admin_app.command("restore")
def admin_restore(
    user_id: str = typer.Argument(..., help="Full user UUID from: agentcraft admin users"),
) -> None:
    """Undo a delete — the account returns with its old password, status, and workspaces."""
    require_auth()
    with client() as c:
        r = c.post(f"/api/v1/admin/users/{user_id}/restore")
        _raise_for_status(r)
        data = r.json()
        u = data.get("user") or {}
        console.print(
            f"[green]Restored[/green] {u.get('email')} ({u.get('status')})  "
            f"id={_full_id(u.get('id'))}"
        )
        console.print(f"[dim]{data.get('message') or ''}[/dim]")


@admin_app.command("purge")
def admin_purge(
    user_id: str = typer.Argument(..., help="Full user UUID of a deleted account"),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt (for scripts)"
    ),
) -> None:
    """
    Erase a deleted account for good — the archive and every workspace it held.

    Unlike `delete`, this cannot be undone: the id is gone, and the person gets the
    ordinary "invalid email or password" instead of a deletion notice.
    """
    require_auth()
    with client() as c:
        target = _find_admin_user(c, user_id)
        if (target.get("status") or "") != "deleted":
            console.print(
                f"[red]{target.get('email')} is not deleted[/red] "
                f"(status {target.get('status')}). Delete it first with "
                "[bold]agentcraft admin delete[/bold]."
            )
            raise typer.Exit(1)

        held = int(target.get("session_count") or 0)
        console.print(
            f"[red]Erasing[/red] {target.get('name')} <{target.get('email')}> "
            f"· deleted {target.get('deleted_at') or '?'} "
            f"· {held} held workspace{'' if held == 1 else 's'}"
        )
        if not yes and not Confirm.ask(
            "This is permanent — restore will no longer be possible. Continue?", default=False
        ):
            console.print("[dim]Cancelled — nothing was erased.[/dim]")
            raise typer.Exit(0)

        r = c.request("DELETE", f"/api/v1/admin/users/{user_id}/purge")
        _raise_for_status(r)
        data = r.json()
        console.print(f"[red]Erased[/red] {data.get('email')}  id={_full_id(data.get('id'))}")
        console.print(f"[dim]{data.get('message') or ''}[/dim]")


@admin_app.command("diagrams")
def admin_diagrams(
    session_id: str = typer.Argument(
        ..., help="Full session UUID from: agentcraft admin users"
    ),
    out: Path = typer.Option(
        Path("blueprint"), "--out", "-o", help="Directory to write the three PNGs into"
    ),
    version: Optional[int] = typer.Option(
        None,
        "--version",
        "-v",
        help="An earlier version from the change history. Default: current.",
    ),
    save: bool = typer.Option(
        True, "--save/--no-save", help="Write the PNGs, or only print the summary"
    ),
) -> None:
    """
    Read any user's process blueprint and architectural views — the CLI half of the /admin
    panel's two viewers.

    Read-only by design: there is deliberately no admin `generate`, `refine`, `freeze` or
    `delete`. An admin can look at the process a user approved, never redraw or approve one
    on their behalf.

    Goes through the admin-gated routes rather than `agentcraft diagrams show`, because that
    command's endpoints filter by owner and answer 404 for someone else's project.

    SDD.md §3.2's three architectural views come down too, when the session has been
    generated. A session can have a design and no drawn process, or the reverse, so neither
    half is a precondition for the other.
    """
    require_auth()
    with client() as c:
        r = c.get(f"/api/v1/admin/projects/{session_id}/diagrams")
        _raise_for_status(r)
        data = r.json()
        if not data:
            console.print(
                "[yellow]No blueprint for this session.[/yellow] It was generated straight "
                "from the brief, which the wizard allows."
            )
        else:
            _print_diagram_set(
                data,
                save_hint=f"agentcraft admin diagrams {session_id} --version N",
            )
            if save:
                for path, px in _save_diagram_pngs(
                    c,
                    session_id,
                    out,
                    version,
                    current_version=data.get("version"),
                    as_admin=True,
                ):
                    console.print(f"[green]Saved[/green] {path}  [dim]{px}[/dim]")

        if not save:
            return
        arch = _save_architecture_pngs(c, session_id, out / "architecture", as_admin=True)
        if not arch:
            console.print(
                "[dim]No architectural views — they are drawn from the generated plan, so "
                "they appear once this session has been generated.[/dim]"
            )
            return
        console.print(
            "\n[bold]Architecture[/bold] [dim]— SDD.md §3.2, drawn from the current design "
            "(no version: there is no earlier revision to ask for)[/dim]"
        )
        for path, px in arch:
            console.print(f"[green]Saved[/green] {path}  [dim]{px}[/dim]")


@admin_app.command("document")
def admin_document(
    session_id: str = typer.Argument(..., help="Session id from `agentcraft admin users`"),
    fmt: str = typer.Option("both", "--format", "-f", help="pdf | docx | both"),
    out: Path = typer.Option(Path("."), "--out", "-o", help="Directory to write into"),
) -> None:
    """
    Download any user's SDD.md as a PDF and/or a Word document.

    The terminal half of the /admin panel's download buttons, and the counterpart to
    `agentcraft plan document` — both routes call one service method, so what a reviewer reads
    here is what the user has.

    Read-only, like the rest of `admin`: the document is laid out from what the session already
    holds and is never regenerated on the owner's behalf.
    """
    require_auth()
    formats = _document_formats(fmt)
    with client() as c:
        console.print(f"[dim]Laying out SDD.md as {', '.join(f.upper() for f in formats)}…[/dim]")
        for path, size in _save_solution_design(c, session_id, out, formats, as_admin=True):
            console.print(f"[green]Saved[/green] {path}  [dim]{size}[/dim]")


@app.command("sessions")
def sessions_cmd(
    state: Optional[str] = typer.Option(
        None,
        "--state",
        "-s",
        help="Filter: exported | ready | in_progress (same as UI KPI filters)",
    ),
    platform: Optional[str] = typer.Option(
        None,
        "--platform",
        help="Filter IDE: cursor | claude_code | windsurf | github_copilot",
    ),
) -> None:
    """List your workspaces in a table (session + agent/skill/rule names)."""
    require_auth()
    state_key = (state or "").strip().lower().replace("-", "_").replace(" ", "_")
    plat_key = (platform or "").strip().lower()
    if state_key and state_key not in {"exported", "ready", "ready_for_review", "in_progress", "active"}:
        console.print("[red]--state must be exported | ready | in_progress[/red]")
        raise typer.Exit(1)
    if plat_key and plat_key not in {"cursor", "claude_code", "windsurf", "github_copilot", "claude"}:
        console.print("[red]--platform must be cursor | claude_code | windsurf | github_copilot[/red]")
        raise typer.Exit(1)
    if plat_key == "claude":
        plat_key = "claude_code"

    with client() as c:
        r = c.get("/api/v1/projects/sessions")
        _raise_for_status(r)
        rows = r.json()
        if not rows:
            console.print("[yellow]No sessions yet. Run agentcraft init or agentcraft wizard.[/yellow]")
            return

        filtered = []
        for s in rows:
            st = str(s.get("state") or "")
            pl = str(s.get("platform") or "")
            if state_key in {"exported"} and st != "EXPORTED":
                continue
            if state_key in {"ready", "ready_for_review"} and st != "READY_FOR_REVIEW":
                continue
            if state_key in {"in_progress", "active"} and st in {"EXPORTED", "READY_FOR_REVIEW"}:
                continue
            if plat_key and pl != plat_key:
                continue
            filtered.append(s)

        table = Table(title="My sessions", show_lines=True, expand=True)
        table.add_column("Session ID (full UUID)", style="cyan", no_wrap=False, overflow="fold")
        table.add_column("Session", style="bold", no_wrap=False, overflow="fold")
        table.add_column("State", no_wrap=True)
        table.add_column("Platform", no_wrap=True)
        table.add_column("#A/#S/#R", justify="center", no_wrap=True)
        table.add_column("Agents", style="green", no_wrap=False, overflow="fold")
        table.add_column("Skills", style="blue", no_wrap=False, overflow="fold")
        table.add_column("Rules", style="magenta", no_wrap=False, overflow="fold")
        # Mirrors the Documents chips in the UI's session detail panel.
        table.add_column("Documents", style="bright_magenta", no_wrap=False, overflow="fold")

        for s in filtered:
            plat = s.get("platform") or "-"
            agents = s.get("agent_names") or []
            skills = s.get("skill_names") or []
            rules = s.get("rule_names") or []
            docs = s.get("document_names") or []
            counts = (
                f"{s.get('agent_count', len(agents))}/"
                f"{s.get('skill_count', len(skills))}/"
                f"{s.get('rule_count', len(rules))}"
            )
            sid = _full_id(s.get("id"))
            table.add_row(
                sid,
                s.get("name") or "",
                str(s.get("state") or "-"),
                _platform_label(plat),
                counts,
                _join_names(agents),
                _join_names(skills),
                _join_names(rules),
                _join_names(docs),
            )

        console.print(table)
        console.print(f"[dim]Showing {len(filtered)}/{len(rows)}[/dim]")
        console.print("Open one: [bold]agentcraft status -p <Session ID>[/bold]")
        console.print("Delete: [bold]agentcraft delete-session <Session ID>[/bold]")
        console.print(
            "[dim]#A/#S/#R = agents / skills / rules. Every IDE exports rules "
            "(.claude/rules/*.md · .cursor/rules/*.mdc · .windsurf/rules/*.md · .github/instructions/*.instructions.md). "
            "Filter: --state exported|ready|in_progress  --platform cursor|claude_code|windsurf|github_copilot[/dim]"
        )

@app.command("delete-session")
def delete_session_cmd(
    project_id: str = typer.Argument(..., help="Full project/session UUID"),
) -> None:
    """Delete one of your workspaces (same as UI Delete)."""
    require_auth()
    with client() as c:
        r = c.delete(f"/api/v1/projects/{project_id}")
        _raise_for_status(r)
        console.print(f"[green]Deleted[/green] session {project_id}")
        if CURRENT_FILE.exists() and CURRENT_FILE.read_text(encoding="utf-8").strip() == project_id:
            CURRENT_FILE.unlink(missing_ok=True)
            console.print("[dim]Cleared active project pointer.[/dim]")


# ── Core flow ────────────────────────────────────────────────────────────────


@app.command()
def health() -> None:
    """Check API health (no auth required)."""
    with client() as c:
        r = c.get("/health")
        _raise_for_status(r)
        # Name the URL: `health` is the command people run when something is wrong, and
        # "which API did it just talk to" is usually the question behind it.
        console.print(Panel(json.dumps(r.json(), indent=2), title=f"Health — {api_url()}"))


@app.command("init")
def init_cmd(
    name: str = typer.Option("Untitled Project", "--name", "-n"),
) -> None:
    """Create a new project (state CREATED)."""
    require_auth()
    with client() as c:
        r = c.post("/api/v1/projects", json={"name": name})
        _raise_for_status(r)
        data = r.json()
        save_current(data["id"])
        console.print(f"[green]Created[/green] {data['id']}")
        show_status(data)


@app.command()
def status(project_id: Optional[str] = typer.Option(None, "--project-id", "-p")) -> None:
    """Show state, path, platform, blockers."""
    require_auth()
    pid = current_id(project_id)
    with client() as c:
        r = c.get(f"/api/v1/projects/{pid}")
        _raise_for_status(r)
        show_status(r.json())


@app.command("path")
def path_cmd(
    kind: str = typer.Argument(..., help="docs | interview"),
    project_id: Optional[str] = typer.Option(None, "--project-id", "-p"),
) -> None:
    """Choose path from CREATED."""
    require_auth()
    if kind not in {"docs", "interview"}:
        console.print("[red]path must be docs or interview[/red]")
        raise typer.Exit(1)
    pid = current_id(project_id)
    with client() as c:
        r = c.post(f"/api/v1/projects/{pid}/path", json={"path": kind})
        _raise_for_status(r)
        show_status(r.json())


@app.command("rollback")
def rollback_cmd(
    project_id: Optional[str] = typer.Option(None, "--project-id", "-p"),
) -> None:
    """Go back one pre-generate wizard step (path / docs / interview / IDE). Locked after generate."""
    require_auth()
    pid = current_id(project_id)
    with client() as c:
        r = c.post(f"/api/v1/projects/{pid}/rollback", json={})
        _raise_for_status(r)
        show_status(r.json())


@docs_app.command("statement")
def docs_statement(
    text: str = typer.Argument(...),
    project_id: Optional[str] = typer.Option(None, "--project-id", "-p"),
) -> None:
    """Deprecated alone — documents are also required. Use: docs add file.pdf -s \"...\" """
    console.print(
        "[yellow]Problem statement alone is no longer enough.[/yellow]\n"
        "Upload matching documents with:\n"
        '  [bold]agentcraft docs add brief.pdf notes.md -s "your problem statement"[/bold]'
    )
    raise typer.Exit(1)


@docs_app.command("expand")
def docs_expand(
    text: Optional[str] = typer.Argument(None, help="Short seed to expand (optional)"),
) -> None:
    """Deprecated — expand belongs to the interview path (same as UI). Use: interview expand"""
    console.print(
        "[yellow]Expand is interview-only (same as UI).[/yellow]\n"
        "Use: [bold]agentcraft interview expand \"your short idea\"[/bold]"
    )
    if text:
        console.print("[dim]Forwarding to interview expand…[/dim]")
        interview_expand(text)
        return
    raise typer.Exit(1)


def _expand_brief_seed(seed: str) -> str:
    with client() as c:
        r = c.post("/api/v1/projects/meta/expand-brief", json={"seed": seed})
        _raise_for_status(r)
        data = r.json()
        return data.get("expanded") or ""


@interview_app.command("expand")
def interview_expand(
    text: Optional[str] = typer.Argument(None, help="Short seed to expand (optional)"),
) -> None:
    """Expand a short idea into a detailed brief (same as UI Expand on interview)."""
    require_auth()
    seed = (text or Prompt.ask("Short problem seed")).strip()
    if not seed:
        console.print("[red]Seed required[/red]")
        raise typer.Exit(1)
    expanded = _expand_brief_seed(seed)
    console.print(Panel(expanded, title="Expanded brief", expand=True))
    console.print(
        "[dim]Paste into an interview answer:[/dim] "
        "[bold]agentcraft interview answer --id <qid> --text \"...\"[/bold]\n"
        "Or use [bold]agentcraft interview[/bold] / [bold]agentcraft wizard[/bold] and paste when prompted."
    )

@docs_app.command("add")
def docs_add(
    files: list[Path] = typer.Argument(...),
    statement: str = typer.Option(..., "--statement", "-s", help="Required problem statement"),
    project_id: Optional[str] = typer.Option(None, "--project-id", "-p"),
) -> None:
    """Upload one or more documents with a required problem statement (Path A)."""
    require_auth()
    if not statement.strip():
        console.print("[red]--statement is required and must match the documents[/red]")
        raise typer.Exit(1)
    if not files:
        console.print(f"[red]Provide at least one document ({ALLOWED_DOC_LABEL})[/red]")
        raise typer.Exit(1)
    for f in files:
        if f.suffix.lower() not in ALLOWED_DOC_EXTS:
            console.print(
                f"[red]Unsupported file type: {f.name}[/red]  allowed: {ALLOWED_DOC_LABEL}"
            )
            raise typer.Exit(1)
    pid = current_id(project_id)
    multipart = []
    file_handles = []
    try:
        for f in files:
            fh = open(f, "rb")
            file_handles.append(fh)
            multipart.append(("files", (f.name, fh)))
        with client() as c:
            r = c.post(
                f"/api/v1/projects/{pid}/documents",
                data={"problem_statement": statement},
                files=multipart,
            )
            _raise_for_status(r)
            show_status(r.json())
            console.print("[dim]Saved documents: [bold]agentcraft docs list[/bold][/dim]")
    finally:
        for fh in file_handles:
            fh.close()


@docs_app.command("list")
def docs_list(project_id: Optional[str] = typer.Option(None, "--project-id", "-p")) -> None:
    """List the documents saved with this project (mirrors the UI's document list)."""
    require_auth()
    pid = current_id(project_id)
    with client() as c:
        r = c.get(f"/api/v1/projects/{pid}")
        _raise_for_status(r)
        data = r.json()

    docs = ((data.get("brief") or {}).get("documents")) or []
    if not docs:
        console.print(
            "[yellow]No documents saved for this project.[/yellow] "
            "Upload with: agentcraft docs add <files> -s \"<statement>\""
        )
        return

    table = Table(title=f"Documents · {data.get('name') or pid}", show_header=True, expand=True)
    table.add_column("#", style="dim", no_wrap=True)
    table.add_column("File", overflow="fold")
    table.add_column("Type", no_wrap=True)
    table.add_column("Size", justify="right", no_wrap=True)
    table.add_column("Extracted", justify="right", no_wrap=True)
    table.add_column("Uploaded", overflow="fold")
    for i, d in enumerate(docs, start=1):
        d = d or {}
        table.add_row(
            str(i),
            str(d.get("filename") or "-"),
            str(d.get("content_type") or "-"),
            # Historical uploads have no recorded byte size.
            _human_bytes(d.get("size_bytes")) or "[dim]—[/dim]",
            f"{int(d.get('chars_extracted') or 0):,} chars",
            str(d.get("uploaded_at") or "[dim]—[/dim]"),
        )
    console.print(table)


@interview_app.callback(invoke_without_command=True)
def interview_interactive(
    ctx: typer.Context,
    project_id: Optional[str] = typer.Option(None, "--project-id", "-p"),
) -> None:
    """Interactive Q&A loop for Path B."""
    if ctx.invoked_subcommand is not None:
        return
    require_auth()
    pid = current_id(project_id)
    with client() as c:
        while True:
            r = c.get(f"/api/v1/projects/{pid}")
            _raise_for_status(r)
            data = r.json()
            if data["state"] != "AWAITING_INTERVIEW":
                show_status(data)
                return
            pending = [q for q in data.get("interview", []) if not q.get("answer")]
            if not pending:
                show_status(data)
                return
            q = pending[0]
            optional = q["id"] != "goal"
            title = f"{q['id']} (optional — blank to skip)" if optional else q["id"]
            console.print(Panel(q["prompt"], title=title))
            ans = Prompt.ask("Your answer", default="" if optional else None)
            if optional and not (ans or "").strip():
                ans = SKIP_ANSWER
            elif not (ans or "").strip():
                console.print("[red]Problem statement is required[/red]")
                continue
            r2 = c.post(
                f"/api/v1/projects/{pid}/interview",
                json={"answers": {q["id"]: ans}},
            )
            _raise_for_status(r2)


@interview_app.command("answer")
def interview_answer(
    id: str = typer.Option(..., "--id"),
    text: str = typer.Option(..., "--text"),
    project_id: Optional[str] = typer.Option(None, "--project-id", "-p"),
) -> None:
    """Submit a single interview answer."""
    require_auth()
    pid = current_id(project_id)
    with client() as c:
        r = c.post(
            f"/api/v1/projects/{pid}/interview",
            json={"answers": {id: text}},
        )
        _raise_for_status(r)
        show_status(r.json())


@platform_app.command("list")
def platform_list() -> None:
    """List supported IDEs."""
    with client() as c:
        r = c.get("/api/v1/projects/meta/platforms")
        _raise_for_status(r)
        for p in r.json():
            console.print(f"[bold]{p['id']}[/bold] - {p['label']}: {p['blurb']}")


@platform_app.command("set")
def platform_set(
    name: str = typer.Argument(..., help="claude_code | cursor | windsurf | github_copilot"),
    project_id: Optional[str] = typer.Option(None, "--project-id", "-p"),
) -> None:
    """Select IDE platform."""
    require_auth()
    pid = current_id(project_id)
    with client() as c:
        r = c.post(f"/api/v1/projects/{pid}/platform", json={"platform": name})
        _raise_for_status(r)
        show_status(r.json())


# ── Process blueprint ─────────────────────────────────────────────────────────────
#
# Same endpoints the UI drives, with wait=true so a command returns finished work instead
# of a job id. The PNGs are written to disk because a terminal cannot show them inline —
# `--out` defaults to ./blueprint so `diagrams generate` leaves three files to open.

DIAGRAM_STEMS = {
    "sipoc": "sipoc",
    "flow": "process-flow",
    "swimlane": "swimlane",
}


def _diagram_filename(kind: str, version: Optional[int]) -> str:
    """`sipoc-v2.png` — the same name the exported workspace uses.

    Kept identical to the backend's `diagrams/views.py::export_filename` on purpose: the
    file this writes and the file in the zip's `docs/diagrams/` are then the same file, so
    a saved set can be matched to the version it was drawn from, and saving again after a
    revision cannot quietly overwrite a set someone has already reviewed.
    """
    stem = DIAGRAM_STEMS[kind]
    return f"{stem}-v{int(version)}.png" if version else f"{stem}.png"

# What each view answers, in the same words the UI's Blueprint step uses. Three PNGs of the
# same process is confusing without this: the names describe the notation, not the question,
# and a reader meeting SIPOC for the first time cannot tell why they are being shown it.
DIAGRAM_PURPOSE = [
    (
        "SIPOC",
        "scope on one page — who hands work in, what the process turns it into, "
        "who receives it, and how it is measured. Read this first.",
    ),
    (
        "Process flow",
        "the order of work — what happens after what, where a decision splits the path, "
        "and where a rejection sends work back for rework.",
    ),
    (
        "Swimlane",
        "who owns what — the same steps in each team or system lane, so every handoff "
        "between them, and every wait it causes, is visible.",
    ),
]


def _print_view_legend() -> None:
    """The three views and what each is for.

    A borderless grid rather than printed lines: a wrapped purpose is two rows in a narrow
    terminal, and only a table keeps the second row indented under the first.
    """
    console.print("[bold]Three views of the same process[/bold]")
    legend = Table(show_header=False, box=None, pad_edge=False, padding=(0, 2, 0, 0))
    legend.add_column("View", style="bold", no_wrap=True)
    legend.add_column("For", style="dim", overflow="fold")
    for label, purpose in DIAGRAM_PURPOSE:
        legend.add_row(f"  {label}", purpose)
    console.print(legend)


# ── What a follow-up is allowed to change ────────────────────────────────
#
# All three views are drawn from one process model, so "change only this diagram" cannot mean
# "change the process for one view" — that would leave the views contradicting each other. It
# means the layout of that one picture, with the process left alone. `all` is the other kind of
# change: the process is revised, all three views follow it, and so does generation.

_DIAGRAM_SCOPES = ("all", "sipoc", "flow", "swimlane")

_SCOPE_VIEW_NAMES = {"sipoc": "SIPOC", "flow": "process flow", "swimlane": "swimlane"}


def _scope_label(scope: str | None) -> str:
    """`All three views` / `SIPOC only`, for the change history."""
    key = (scope or "all").strip().lower()
    if key == "all" or key not in _SCOPE_VIEW_NAMES:
        return "all three views"
    return f"{_SCOPE_VIEW_NAMES[key]} only"


def _scope_progress(scope: str) -> str:
    """The one line printed before the call, so a long wait says what it is doing."""
    if scope == "all":
        return "Revising the process, then redrawing all three views"
    return (
        f"Re-laying out the {_SCOPE_VIEW_NAMES.get(scope, scope)} view only — "
        "the process is not being changed"
    )


def _no_change_advice(r: httpx.Response) -> Optional[str]:
    """The server's "your follow-up changed nothing" reply, if that is what came back.

    A 422 whose message opens with "Nothing changed" is advice, not a fault: the layout call
    answered but moved nothing, so the server deliberately kept the current version rather than
    recording an identical one. Printed amber, and `quickstart` stays in its loop so the user can
    say the same thing more concretely instead of starting the whole command again.
    """
    if r.status_code != 422:
        return None
    try:
        message = str((r.json() or {}).get("message") or "")
    except Exception:
        return None
    return message if message.startswith("Nothing changed") else None


def _sipoc_line(items: list) -> str:
    names = [str((i or {}).get("name") or "").strip() for i in items or []]
    return " → ".join(n for n in names if n) or "-"


def _print_diagram_set(data: dict, *, save_hint: str = "agentcraft diagrams save --version N") -> None:
    """Everything the UI shows on its Blueprint step, as text.

    `save_hint` is the command the change history points at for redrawing an older version.
    The super admin reads blueprints through a different command, and printing the owner's
    command to someone who would get a 404 from it is worse than printing nothing.
    """
    model = data.get("model") or {}
    steps = model.get("steps") or []
    sipoc = data.get("sipoc") or {}
    swim = data.get("swimlane") or {}

    _print_view_legend()

    table = Table(title="Process blueprint", show_header=True, expand=True)
    table.add_column("Field", style="bold", no_wrap=True)
    table.add_column("Value", overflow="fold", no_wrap=False)
    table.add_row("Version", f"v{data.get('version') or 1}")
    table.add_row("Approved", "yes" if data.get("frozen") else "no — generation is blocked")
    table.add_row("Drawn by", "model" if data.get("llm") else "deterministic fallback")
    table.add_row("Title", str(model.get("title") or "-"))
    table.add_row("Scope", str(model.get("scope") or "-"))
    decisions = sum(1 for s in steps if (s or {}).get("kind") == "decision")
    table.add_row(
        "Size",
        f"{len(steps)} steps · {decisions} decisions · "
        f"{len(model.get('actors') or [])} actors · {len(model.get('edges') or [])} handoffs",
    )
    table.add_row("Lanes", ", ".join(swim.get("lanes") or []) or "-")
    # The stage bands across the top of the swimlane. Adjacent columns are merged when a
    # process has more phases than the picture can hold, so this is the merged list, not
    # one name per step.
    stages = [str(s or "").strip() for s in (swim.get("stages") or [])]
    table.add_row(
        "Stages",
        " │ ".join(f"{i}. {s}" for i, s in enumerate(stages, start=1) if s) or "-",
    )
    console.print(table)

    sip = Table(title="SIPOC", show_header=True, expand=True)
    sip.add_column("Column", style="bold", no_wrap=True)
    sip.add_column("Entries", overflow="fold", no_wrap=False)
    for key in ("suppliers", "inputs", "process", "outputs", "customers"):
        sip.add_row(key.title(), _sipoc_line(sipoc.get(key) or []))
    console.print(sip)

    # Systems and description are here for the same reason the UI lists them in full: the
    # PNG ellipsises a long label to keep its box readable, so the picture is not the place
    # to check what the model actually wrote — and this text is what generation reads.
    flow = Table(title="Process steps", show_header=True, expand=True)
    flow.add_column("#", style="bold", no_wrap=True)
    flow.add_column("Step", overflow="fold")
    flow.add_column("Kind", no_wrap=True)
    flow.add_column("Actor", overflow="fold")
    flow.add_column("Systems", overflow="fold")
    flow.add_column("Detail", overflow="fold")
    for i, s in enumerate(steps, start=1):
        flow.add_row(
            str(i),
            str(s.get("name") or "-"),
            str(s.get("kind") or "-"),
            str(s.get("actor") or "-"),
            ", ".join(s.get("systems") or []) or "-",
            str(s.get("description") or "-"),
        )
    console.print(flow)

    for label, key in (("Assumptions", "assumptions"), ("Measures", "kpis")):
        entries = model.get(key) or []
        if entries:
            console.print(f"[bold]{label}[/bold]")
            for e in entries:
                console.print(f"  • {e}")
    warnings = data.get("warnings") or []
    if warnings:
        console.print("[yellow]Repairs and gaps[/yellow]")
        for w in warnings:
            console.print(f"  • {w}")
    revisions = data.get("revisions") or []
    if revisions:
        console.print("[bold]Change history[/bold]")
        for rev in revisions:
            # v1 has no instruction — it is the first draw, not a change.
            asked = str(rev.get("instruction") or "").strip() or "first draw"
            # `viewable` says the views needed to redraw that version are still stored, which
            # is what `diagrams save --version` needs. Older entries stay as a record.
            note = "" if rev.get("viewable") is not False else " [dim](diagrams not kept)[/dim]"
            console.print(f"  v{rev.get('version')}: {asked}{note}")
            console.print(f"     [dim]{rev.get('summary')}[/dim]")
            # Which kind of change it was. A version number alone cannot distinguish a rewritten
            # process — which moved the generated agents with it — from a re-drawn picture.
            if asked != "first draw":
                console.print(f"     [dim]applied to: {_scope_label(rev.get('scope'))}[/dim]")
        console.print(
            f"[dim]Write an earlier version's PNGs with: {save_hint}[/dim]"
        )


def _png_pixels(data: bytes) -> str:
    """`WxH` straight from the IHDR chunk, or `''` if this is not a PNG after all."""
    try:
        width, height = struct.unpack(">II", data[16:24])
        return f"{int(width)}×{int(height)}"
    except Exception:  # noqa: BLE001
        return ""


def _save_diagram_pngs(
    c: httpx.Client,
    pid: str,
    out: Path,
    version: Optional[int] = None,
    *,
    current_version: Optional[int] = None,
    as_admin: bool = False,
) -> list[tuple[Path, str]]:
    """Write the three renders next to each other so they can be opened and compared.

    The pixel size comes back with each path because the renders are supersampled: a dense
    swimlane is several thousand pixels wide, and that is what makes it readable zoomed in.
    Seeing the size is how you know you got the real render and not a stale cached file.

    Every filename carries its version, current one included, so these are byte-for-byte
    the names the exported workspace uses under `docs/diagrams/`.

    `version` asks for an earlier version from the change history. `current_version` lets a
    caller that already holds the blueprint name the current one without a second request.

    `as_admin` swaps in the admin-gated read-only routes. The ordinary ones filter by owner
    and return 404 for someone else's project, which is right for the studio and useless for
    the super admin reviewing it.
    """
    out.mkdir(parents=True, exist_ok=True)
    base = "/api/v1/admin/projects" if as_admin else "/api/v1/projects"
    stamp = version or current_version or _current_diagram_version(c, pid, as_admin=as_admin)
    written: list[tuple[Path, str]] = []
    for kind in DIAGRAM_STEMS:
        r = c.get(
            f"{base}/{pid}/diagrams/{kind}.png",
            params={"version": version} if version else None,
        )
        _raise_for_status(r)
        target = out / _diagram_filename(kind, stamp)
        target.write_bytes(r.content)
        written.append((target, _png_pixels(r.content)))
    return written


#: SDD.md §3.2's views and the filenames the exported workspace uses under
#: `docs/architecture/` — `ARCH_VIEWS` in `backend/app/services/diagrams/architecture.py`.
#: No `-vN` stamp: these are drawn from the current design, not an approved revision.
ARCHITECTURE_STEMS = {
    "logical": "logical-view",
    "development": "development-view",
    "deployment": "deployment-view",
}


def _save_architecture_pngs(
    c: httpx.Client,
    pid: str,
    out: Path,
    *,
    as_admin: bool = False,
) -> list[tuple[Path, str]]:
    """Write SDD §3.2's three architectural views, named as the zip names them.

    The blueprint's counterpart, and the terminal half of what the /admin panel shows: the
    process a user agreed and the software design that came out of it are two different
    reviews, and until now the CLI could only fetch the first.

    Best effort per view — a session generated before the SDD existed renders nothing, and
    that is a missing picture, not a failed command, so the ones that do render are still
    written. Returns what was written, so an empty list means "there was nothing to draw".
    """
    out.mkdir(parents=True, exist_ok=True)
    base = "/api/v1/admin/projects" if as_admin else "/api/v1/projects"
    suffix = "architecture" if as_admin else "plan/architecture"
    written: list[tuple[Path, str]] = []
    for kind, stem in ARCHITECTURE_STEMS.items():
        r = c.get(f"{base}/{pid}/{suffix}/{kind}.png")
        if r.status_code != 200:
            continue
        target = out / f"{stem}.png"
        target.write_bytes(r.content)
        written.append((target, _png_pixels(r.content)))
    return written


#: The two formats SDD.md downloads as — `DOC_FORMATS` in
#: `backend/app/services/export/documents.py`. `both` is a CLI convenience, not a format.
DOCUMENT_FORMATS = ("pdf", "docx")


def _save_solution_design(
    c: httpx.Client,
    pid: str,
    out: Path,
    formats: tuple[str, ...],
    *,
    as_admin: bool = False,
) -> list[tuple[Path, str]]:
    """Write SDD.md as a PDF and/or a DOCX. Returns `[(path, size)]` for what was written.

    The server lays the document out from the markdown the session holds, with §3.2's three
    architectural views embedded — one parser feeding both emitters, so the file this writes is
    the document the studio previews rather than a third rendering of it.

    Unlike `_save_architecture_pngs` this is not best-effort: a format the caller asked for and
    did not get is a failure worth saying so about, because there is no partial version of a
    document that is any use.
    """
    out.mkdir(parents=True, exist_ok=True)
    base = "/api/v1/admin/projects" if as_admin else "/api/v1/projects"
    suffix = "solution-design" if as_admin else "plan/solution-design"
    written: list[tuple[Path, str]] = []
    for fmt in formats:
        r = c.get(f"{base}/{pid}/{suffix}.{fmt}")
        _raise_for_status(r)
        # The server names the file after the project; the header is taken as read so the CLI
        # and the browser download cannot disagree about it.
        match = re.search(r'filename="([^"]+)"', r.headers.get("content-disposition", ""))
        target = out / (match.group(1) if match else f"SDD.{fmt}")
        target.write_bytes(r.content)
        written.append((target, f"{len(r.content) / 1024:.0f} KB"))
    return written


def _document_formats(fmt: str) -> tuple[str, ...]:
    """`pdf` | `docx` | `both` → the formats to fetch. Exits on anything else."""
    key = (fmt or "").strip().lower()
    if key == "both":
        return DOCUMENT_FORMATS
    if key in DOCUMENT_FORMATS:
        return (key,)
    console.print("[red]--format must be pdf | docx | both[/red]")
    raise typer.Exit(1)


def _current_diagram_version(c: httpx.Client, pid: str, *, as_admin: bool = False) -> Optional[int]:
    """The version the filenames should carry when the caller did not say.

    Best effort: if this cannot be read the files are still written, just without the `-vN`
    stamp. A failed lookup is not a reason to lose the renders.
    """
    base = "/api/v1/admin/projects" if as_admin else "/api/v1/projects"
    try:
        r = c.get(f"{base}/{pid}/diagrams")
        if r.status_code != 200:
            return None
        return (r.json() or {}).get("version")
    except Exception:  # noqa: BLE001
        return None


@diagrams_app.command("generate")
def diagrams_generate(
    demo: bool = typer.Option(
        False,
        "--demo",
        help="Offline test escape hatch only (skip Bedrock). UI always uses live Bedrock.",
    ),
    out: Path = typer.Option(Path("blueprint"), "--out", help="Directory for the three PNGs"),
    project_id: Optional[str] = typer.Option(None, "--project-id", "-p"),
) -> None:
    """Draw SIPOC, process flow and swimlane from the brief. Requires CONTEXT_READY."""
    require_auth()
    pid = current_id(project_id)
    with client() as c:
        console.print("[dim]Reading the process, then drawing the three views…[/dim]")
        r = c.post(f"/api/v1/projects/{pid}/diagrams", params={"demo": demo, "wait": True})
        _raise_for_status(r)
        data = r.json()
        _print_diagram_set(data)
        for path, px in _save_diagram_pngs(
            c, pid, out, current_version=data.get("version")
        ):
            console.print(f"[green]Saved[/green] {path.resolve()} [dim]{px}[/dim]")
        console.print(
            "[dim]Change something with: agentcraft diagrams refine \"...\" · "
            "approve with: agentcraft diagrams freeze[/dim]"
        )


@diagrams_app.command("show")
def diagrams_show(
    as_json: bool = typer.Option(False, "--json", help="Print the raw blueprint JSON"),
    project_id: Optional[str] = typer.Option(None, "--project-id", "-p"),
) -> None:
    """Print the stored blueprint."""
    require_auth()
    pid = current_id(project_id)
    with client() as c:
        r = c.get(f"/api/v1/projects/{pid}/diagrams")
        _raise_for_status(r)
        data = r.json()
        if not data:
            console.print(
                "No blueprint for this project. Draw one with: agentcraft diagrams generate\n"
                "[dim]Projects without a blueprint generate straight from the brief.[/dim]"
            )
            return
        if as_json:
            console.print_json(data=data)
            return
        _print_diagram_set(data)


@diagrams_app.command("save")
def diagrams_save(
    out: Path = typer.Option(Path("blueprint"), "--out", help="Directory for the three PNGs"),
    version: Optional[int] = typer.Option(
        None,
        "--version",
        "-v",
        help="An earlier version from the change history (see: diagrams show). Default: current.",
    ),
    project_id: Optional[str] = typer.Option(None, "--project-id", "-p"),
) -> None:
    """Write the three rendered PNGs to a directory."""
    require_auth()
    pid = current_id(project_id)
    with client() as c:
        try:
            written = _save_diagram_pngs(c, pid, out, version)
        except typer.Exit:
            # A version that has aged out of the history 404s. Say which ones are left rather
            # than leaving the user to guess a number.
            if version:
                r = c.get(f"/api/v1/projects/{pid}/diagrams")
                data = r.json() if r.status_code == 200 else None
                kept = [
                    f"v{rev.get('version')}"
                    for rev in ((data or {}).get("revisions") or [])
                    if rev.get("viewable") is not False
                ]
                if kept:
                    console.print(f"[dim]Still available: {', '.join(kept)}[/dim]")
            raise
        for path, px in written:
            console.print(f"[green]Saved[/green] {path.resolve()} [dim]{px}[/dim]")
        # This command prints no blueprint, so without the legend the user is left with three
        # filenames and no reason to open one before another.
        _print_view_legend()


@diagrams_app.command("refine")
def diagrams_refine(
    instruction: str = typer.Argument(..., help='e.g. "the reviewer also checks credit history"'),
    scope: str = typer.Option(
        "all",
        "--scope",
        "--only",
        help="all | sipoc | flow | swimlane — what the change is allowed to touch",
    ),
    out: Path = typer.Option(Path("blueprint"), "--out", help="Directory for the redrawn PNGs"),
    project_id: Optional[str] = typer.Option(None, "--project-id", "-p"),
) -> None:
    """Apply one plain-language change.

    `--scope all` (the default) revises the process itself, so all three views are redrawn from
    it and generation follows the new process. `--scope sipoc|flow|swimlane` re-draws that one
    view — grouping, ordering, wording, level of detail — and leaves the process, the other two
    views and therefore the generated agents exactly as they were.
    """
    require_auth()
    pid = current_id(project_id)
    scope = (scope or "all").strip().lower()
    if scope not in _DIAGRAM_SCOPES:
        console.print(
            f"[red]Unknown scope[/red] '{scope}'. Use one of: {', '.join(_DIAGRAM_SCOPES)}."
        )
        raise typer.Exit(1)
    with client() as c:
        console.print(f"[dim]{_scope_progress(scope)}…[/dim]")
        r = c.post(
            f"/api/v1/projects/{pid}/diagrams/followup",
            params={"wait": True},
            json={"instruction": instruction, "scope": scope},
        )
        advice = _no_change_advice(r)
        if advice:
            # Nothing to reprint — the blueprint on disk and on the server is the one that was
            # already there, so the PNGs already saved are still current.
            console.print(f"[yellow]{advice}[/yellow]")
            raise typer.Exit(1)
        _raise_for_status(r)
        data = r.json()
        _print_diagram_set(data)
        for path, px in _save_diagram_pngs(
            c, pid, out, current_version=data.get("version")
        ):
            console.print(f"[green]Saved[/green] {path.resolve()} [dim]{px}[/dim]")
        console.print("[dim]A revision reopens the blueprint — freeze it again when it is right.[/dim]")


@diagrams_app.command("freeze")
def diagrams_freeze(project_id: Optional[str] = typer.Option(None, "--project-id", "-p")) -> None:
    """Approve the blueprint. Generation reads the approved process and is blocked until this."""
    require_auth()
    pid = current_id(project_id)
    with client() as c:
        r = c.post(f"/api/v1/projects/{pid}/diagrams/freeze")
        _raise_for_status(r)
        console.print(
            f"[green]Approved[/green] blueprint v{r.json().get('version')} — "
            "agents, skills and rules will follow it."
        )


@diagrams_app.command("unfreeze")
def diagrams_unfreeze(project_id: Optional[str] = typer.Option(None, "--project-id", "-p")) -> None:
    """Reopen the blueprint for changes."""
    require_auth()
    pid = current_id(project_id)
    with client() as c:
        r = c.post(f"/api/v1/projects/{pid}/diagrams/unfreeze")
        _raise_for_status(r)
        console.print(f"Reopened blueprint v{r.json().get('version')} for changes.")


@diagrams_app.command("discard")
def diagrams_discard(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation"),
    project_id: Optional[str] = typer.Option(None, "--project-id", "-p"),
) -> None:
    """Drop the blueprint. The project then generates from the brief alone, as before."""
    require_auth()
    pid = current_id(project_id)
    if not yes and not Confirm.ask("Discard the process blueprint?", default=False):
        raise typer.Exit(0)
    with client() as c:
        r = c.delete(f"/api/v1/projects/{pid}/diagrams")
        _raise_for_status(r)
        show_status(r.json())


@app.command()
def generate(
    demo: bool = typer.Option(
        False,
        "--demo",
        help="Offline test escape hatch only (skip Bedrock). UI always uses live Bedrock.",
    ),
    project_id: Optional[str] = typer.Option(None, "--project-id", "-p"),
) -> None:
    """Generate agents, skills and rules for the selected IDE. Requires PLATFORM_SELECTED."""
    require_auth()
    pid = current_id(project_id)
    with client() as c:
        r = c.post(
            f"/api/v1/projects/{pid}/generate",
            params={"demo": demo, "wait": True},
        )
        _raise_for_status(r)
        data = r.json()
        show_status(data)
        plan = data.get("plan") or {}
        _print_plan_summary(plan, data.get("platform"))


@plan_app.command("show")
def plan_show(project_id: Optional[str] = typer.Option(None, "--project-id", "-p")) -> None:
    """Print current plan JSON."""
    require_auth()
    pid = current_id(project_id)
    with client() as c:
        r = c.get(f"/api/v1/projects/{pid}/plan")
        _raise_for_status(r)
        console.print_json(data=r.json())


@plan_app.command("tree")
def plan_tree(
    project_id: Optional[str] = typer.Option(None, "--project-id", "-p"),
    purposes: bool = typer.Option(True, "--purposes/--no-purposes", help="Show file role table"),
) -> None:
    """Show modular project structure + file purposes (same scaffold as UI export tree)."""
    require_auth()
    pid = current_id(project_id)
    with client() as c:
        r = c.get(f"/api/v1/projects/{pid}/plan")
        _raise_for_status(r)
        plan = r.json()
        # If empty/legacy, hit preview to trigger backend backfill then re-fetch
        if not (plan.get("source_tree") or []):
            prev = c.get(f"/api/v1/projects/{pid}/export/preview")
            if prev.is_success:
                r2 = c.get(f"/api/v1/projects/{pid}/plan")
                if r2.is_success:
                    plan = r2.json()
        _print_source_tree(plan, with_purpose=purposes)


@plan_app.command("document")
def plan_document(
    fmt: str = typer.Option("both", "--format", "-f", help="pdf | docx | both"),
    out: Path = typer.Option(Path("."), "--out", "-o", help="Directory to write into"),
    project_id: Optional[str] = typer.Option(None, "--project-id", "-p"),
) -> None:
    """
    Download SDD.md as a PDF and/or a Word document.

    The same document the studio previews in its Files pane, laid out for paper with §3.2's
    three architectural views embedded — the PDF and the DOCX come off one parse of the
    markdown, so they cannot disagree with each other or with the screen.

    Nothing is generated here: a session whose SDD.md has never been written gets the
    deterministic document rather than a Bedrock call behind a download.
    """
    require_auth()
    pid = current_id(project_id)
    formats = _document_formats(fmt)
    with client() as c:
        console.print(f"[dim]Laying out SDD.md as {', '.join(f.upper() for f in formats)}…[/dim]")
        for path, size in _save_solution_design(c, pid, out, formats):
            console.print(f"[green]Saved[/green] {path}  [dim]{size}[/dim]")


@plan_app.command("edit")
def plan_edit(
    json_file: Path = typer.Option(..., "--json", help="Path to plan JSON"),
    project_id: Optional[str] = typer.Option(None, "--project-id", "-p"),
) -> None:
    """Replace plan from a JSON file."""
    require_auth()
    pid = current_id(project_id)
    payload = json.loads(json_file.read_text(encoding="utf-8"))
    with client() as c:
        r = c.put(f"/api/v1/projects/{pid}/plan", json=payload)
        _raise_for_status(r)
        show_status(r.json())


@app.command("export")
def export_cmd(
    out: Optional[Path] = typer.Option(None, "--out", help="Write directory"),
    zip_path: Optional[Path] = typer.Option(None, "--zip", help="Write zip file"),
    project_id: Optional[str] = typer.Option(None, "--project-id", "-p"),
) -> None:
    """Export generated files to directory or zip."""
    require_auth()
    pid = current_id(project_id)
    with client() as c:
        # The UI generates the detailed WORKBREAKDOWN.md and SDD.md when Review/Export
        # opens, so both are already in the zip by the time anyone clicks Export there. Do
        # the same before writing files, or a CLI export ships the short fallback versions.
        plan_r = c.get(f"/api/v1/projects/{pid}/plan")
        if plan_r.is_success:
            _ensure_generated_docs(c, pid, plan_r.json() or {})
        if out:
            r = c.post(
                f"/api/v1/projects/{pid}/export",
                json={"mode": "directory", "output_path": str(out.resolve())},
            )
            _raise_for_status(r)
            console.print(f"[green]Wrote directory[/green] {out}")
            console.print(r.json())
        else:
            r = c.post(f"/api/v1/projects/{pid}/export", json={"mode": "zip"})
            _raise_for_status(r)
            meta = r.json()
            dr = c.get(f"/api/v1/projects/{pid}/export/download")
            _raise_for_status(dr)
            target = zip_path or Path(f"agentcraft-{pid[:8]}.zip")
            target.write_bytes(dr.content)
            console.print(f"[green]Wrote zip[/green] {target}")
            files = meta.get("files") or []
            app_files = [
                f
                for f in files
                if not str(f).startswith((
                    ".cursor/",
                    ".claude/",
                    ".windsurf/",
                    ".agents/",
                    ".github/agents/",
                    ".github/skills/",
                    ".github/instructions/",
                ))
                and str(f) not in {
                    "README.md",
                    "AGENTS.md",
                    ".mcp.json",
                    ".github/copilot-instructions.md",
                    ".env.example",
                    "requirements.txt",
                }
            ]
            # Two counts, same names the UI and README use — see _source_file_count.
            plan_r = c.get(f"/api/v1/projects/{pid}/plan")
            source_n = None
            if plan_r.is_success:
                source_n = _source_file_count(plan_r.json() or {})
            if source_n is not None:
                console.print(
                    f"Source files: {source_n}  [dim](app scaffold — same as UI Files tab / README)[/dim]"
                )
            console.print(
                f"Workspace files: {len(files)}  "
                "[dim](everything in the zip: scaffold + IDE folder + README + "
                "WORKBREAKDOWN.md + SDD.md + docs/architecture/*.png)[/dim]"
            )
            if app_files and source_n is None:
                console.print(f"[dim]App scaffold files:[/dim] {len(app_files)}  (see README.md)")


@app.command("preview")
def preview_cmd(
    file: Optional[str] = typer.Option(
        None, "--file", "-f", help="Print one file (default: README.md)"
    ),
    list_only: bool = typer.Option(False, "--list", "-l", help="List paths only"),
    depth: Optional[int] = typer.Option(
        None,
        "--depth",
        "-d",
        min=1,
        help="Collapse folders deeper than this (default: show all, like UI 'Expand all')",
    ),
    project_id: Optional[str] = typer.Option(None, "--project-id", "-p"),
) -> None:
    """
    Preview export file tree / contents (same as UI Export workspace).

    `--depth` is the terminal equivalent of the UI's collapsed folders: deeper folders
    print as "(N files)" instead of their contents.
    """
    require_auth()
    pid = current_id(project_id)
    with client() as c:
        # Same as the UI's Review/Export step: fill in the detailed WORKBREAKDOWN.md and
        # SDD.md before showing the workspace, so what is previewed here is what the
        # browser would show.
        plan_r = c.get(f"/api/v1/projects/{pid}/plan")
        if plan_r.is_success:
            _ensure_generated_docs(c, pid, plan_r.json() or {})
        r = c.get(f"/api/v1/projects/{pid}/export/preview")
        _raise_for_status(r)
        data = r.json()
        paths = data.get("files") or list((data.get("contents") or {}).keys())
        contents = data.get("contents") or {}
        if list_only or not file:
            console.print(
                Panel(
                    "\n".join(_ascii_tree_lines(paths, max_depth=depth)),
                    title="Export preview",
                    expand=False,
                )
            )
            plan_r = c.get(f"/api/v1/projects/{pid}/plan")
            source_n = _source_file_count(plan_r.json() or {}) if plan_r.is_success else 0
            hint = (
                "— expand one level more: [bold]agentcraft preview -l -d "
                f"{depth + 1}[/bold]"
                if depth
                else "— show one: [bold]agentcraft preview -f README.md[/bold]"
            )
            console.print(
                f"[dim]Source files:[/dim] {source_n}  ·  "
                f"[dim]Workspace files:[/dim] {len(paths)}  {hint}"
            )
            _print_generated_root_files(paths)
        target = file or "README.md"
        # resolve exact key
        key = next((p for p in paths if p == target or p.endswith("/" + target) or p.endswith(target)), None)
        if key is None and not list_only:
            console.print(f"[red]File not found:[/red] {target}")
            raise typer.Exit(1)
        if key is not None and not list_only:
            body = contents.get(key) or ""
            # File bodies are data, not markup. Rich would read `*.py[cod]` in a
            # .gitignore (or a `[tool.x]` TOML header) as a style tag and swallow it.
            console.print(Panel(Text(body), title=key, expand=True))


@config_app.command("show")
def config_show() -> None:
    """Show the Bedrock model in use (requires login)."""
    require_auth()
    with client() as c:
        r = c.get("/api/v1/settings/llm")
        _raise_for_status(r)
        console.print_json(data=r.json())


@config_app.command("set-model")
def config_set_model(model: str = typer.Argument(...)) -> None:
    """Repoint generation at another Bedrock model — super admin only.

    Process-wide, not per user: this changes the model every workspace generates with,
    which is why the API restricts it to an admin.
    """
    require_auth()
    with client() as c:
        r = c.patch("/api/v1/settings/llm", json={"model": model})
        _raise_for_status(r)
        console.print_json(data=r.json())


@app.command()
def wizard() -> None:
    """Fully interactive flow across all states (auth + generate + export)."""
    console.print(Panel("AgentCraft Wizard", subtitle="Interactive setup"))
    if not load_token():
        console.print("You need an approved account before creating projects.")
        if Confirm.ask("Already approved? Log in now", default=True):
            auth_login()
        else:
            auth_signup()
            console.print("Ask the super admin to approve you, then log in.")
            auth_login()
    else:
        with client() as c:
            me = c.get("/api/v1/auth/me")
            if me.status_code in (401, 403):
                console.print("[yellow]Stored session invalid - please log in again.[/yellow]")
                auth_login()
            elif me.is_success:
                u = me.json()
                console.print(f"Logged in as [bold]{u.get('email')}[/bold]")

    name = Prompt.ask("Project name", default="Untitled Project")
    with client() as c:
        r = c.post("/api/v1/projects", json={"name": name})
        _raise_for_status(r)
        data = r.json()
        pid = data["id"]
        save_current(pid)
        console.print(f"Project id: [bold]{pid}[/bold]")

        choice = Prompt.ask(
            "Do you have PRD/BRD documents?",
            choices=["yes", "no"],
            default="no",
        )
        if choice == "yes":
            r = c.post(f"/api/v1/projects/{pid}/path", json={"path": "docs"})
            _raise_for_status(r)
            statement = Prompt.ask("Problem statement (required)")
            paths_raw = Prompt.ask(
                f"Document paths (required, comma-separated; {ALLOWED_DOC_LABEL})",
            )
            path_items = [p.strip() for p in paths_raw.split(",") if p.strip()]
            if not statement.strip() or not path_items:
                console.print("[red]Both problem statement and at least one document are required[/red]")
                raise typer.Exit(1)
            files = []
            handles = []
            try:
                for path_str in path_items:
                    p = Path(path_str)
                    fh = open(p, "rb")
                    handles.append(fh)
                    files.append(("files", (p.name, fh)))
                r = c.post(
                    f"/api/v1/projects/{pid}/documents",
                    data={"problem_statement": statement},
                    files=files,
                )
                _raise_for_status(r)
            finally:
                for fh in handles:
                    fh.close()
        else:
            r = c.post(f"/api/v1/projects/{pid}/path", json={"path": "interview"})
            _raise_for_status(r)
            while True:
                st_r = c.get(f"/api/v1/projects/{pid}")
                _raise_for_status(st_r)
                st = st_r.json()
                if st["state"] != "AWAITING_INTERVIEW":
                    break
                pending = [q for q in st.get("interview", []) if not q.get("answer")]
                if not pending:
                    break
                q = pending[0]
                optional = q.get("id") != "goal"
                title = f"{q['id']} (optional — blank to skip)" if optional else q["id"]
                console.print(Panel(q["prompt"], title=title))
                if not optional:
                    if Confirm.ask("Expand a short idea into a detailed brief first? (same as UI)", default=False):
                        seed = Prompt.ask("Short seed")
                        if seed.strip():
                            expanded = _expand_brief_seed(seed.strip())
                            console.print(Panel(expanded, title="Expanded brief", expand=True))
                            if Confirm.ask("Use expanded brief as this answer?", default=True):
                                ans = expanded
                            else:
                                ans = Prompt.ask("Answer")
                        else:
                            ans = Prompt.ask("Answer")
                    else:
                        ans = Prompt.ask("Answer")
                    if not (ans or "").strip():
                        console.print("[red]Problem statement is required[/red]")
                        continue
                else:
                    ans = Prompt.ask("Answer", default="")
                    if not (ans or "").strip():
                        ans = SKIP_ANSWER
                ans_r = c.post(
                    f"/api/v1/projects/{pid}/interview",
                    json={"answers": {q["id"]: ans}},
                )
                _raise_for_status(ans_r)

        # Blueprint before IDE, same order as the UI: the diagrams are what generation
        # follows, so they are reviewed first. Declining leaves the pre-existing flow —
        # generation reads the brief directly.
        if Confirm.ask(
            "Draw the process blueprint (SIPOC, process flow, swimlane) first?",
            default=True,
        ):
            console.print("[dim]Reading the process, then drawing the three views…[/dim]")
            br = c.post(f"/api/v1/projects/{pid}/diagrams", params={"demo": False, "wait": True})
            _raise_for_status(br)
            bp = br.json()
            _print_diagram_set(bp)
            out = Path("blueprint")
            for path, px in _save_diagram_pngs(
                c, pid, out, current_version=bp.get("version")
            ):
                console.print(f"[green]Saved[/green] {path.resolve()} [dim]{px}[/dim]")
            while True:
                console.print("[dim]Open the PNGs above, then approve or describe a change.[/dim]")
                change = Prompt.ask("Change to make (blank to approve)", default="")
                if not change.strip():
                    break
                # The same question the wizard asks, for the same reason: "more detail in the
                # SIPOC" is a request about one picture, while "the reviewer also checks credit
                # history" is a request about the process. Guessing wrong either rewrites a
                # process the user only wanted re-drawn, or re-draws a picture when they wanted
                # the process — and the process is what generation reads.
                console.print(
                    "[dim]all = revise the process, redraw all three views (generation follows"
                    " it) · sipoc/flow/swimlane = re-draw that one view, process untouched[/dim]"
                )
                fscope = Prompt.ask(
                    "Apply to",
                    choices=list(_DIAGRAM_SCOPES),
                    default="all",
                )
                console.print(f"[dim]{_scope_progress(fscope)}…[/dim]")
                fr = c.post(
                    f"/api/v1/projects/{pid}/diagrams/followup",
                    params={"wait": True},
                    json={"instruction": change.strip(), "scope": fscope},
                )
                advice = _no_change_advice(fr)
                if advice:
                    # Not fatal: the blueprint is untouched, so loop round and let them say it
                    # again with the detail the message asks for.
                    console.print(f"[yellow]{advice}[/yellow]")
                    continue
                _raise_for_status(fr)
                bp = fr.json()
                _print_diagram_set(bp)
                for path, px in _save_diagram_pngs(
                    c, pid, out, current_version=bp.get("version")
                ):
                    console.print(f"[green]Saved[/green] {path.resolve()} [dim]{px}[/dim]")
            zr = c.post(f"/api/v1/projects/{pid}/diagrams/freeze")
            _raise_for_status(zr)
            console.print(
                f"[green]Approved[/green] blueprint v{zr.json().get('version')} — "
                "generation will follow it."
            )

        plat = Prompt.ask(
            "Which IDE?",
            choices=["claude_code", "cursor", "windsurf", "github_copilot"],
            default="cursor",
        )
        r = c.post(f"/api/v1/projects/{pid}/platform", json={"platform": plat})
        _raise_for_status(r)

        console.print("[dim]Generating with live Bedrock (same as UI)…[/dim]")
        r = c.post(
            f"/api/v1/projects/{pid}/generate",
            params={"demo": False, "wait": True},
        )
        _raise_for_status(r)
        show_status(r.json())
        plan = (r.json().get("plan") or {})
        # Matches the UI reaching its Review step: the detailed WORKBREAKDOWN.md and
        # SDD.md are generated here, not left to the export.
        plan = _ensure_generated_docs(c, pid, plan)
        _print_plan_summary(plan, plat)

        if Confirm.ask("Show project structure?", default=True):
            _print_source_tree(plan, with_purpose=True)

        if Confirm.ask("Export zip now?", default=True):
            er = c.post(f"/api/v1/projects/{pid}/export", json={"mode": "zip"})
            _raise_for_status(er)
            dr = c.get(f"/api/v1/projects/{pid}/export/download")
            _raise_for_status(dr)
            target = Path(f"agentcraft-{plat}.zip")
            target.write_bytes(dr.content)
            console.print(f"[green]Saved[/green] {target.resolve()}")
            meta = er.json()
            files = meta.get("files") or []
            source_n = _source_file_count(plan)
            console.print(f"Source files: {source_n}  [dim](app scaffold — same as UI)[/dim]")
            console.print(
                f"Workspace files: {len(files)}  [dim](everything in the zip)[/dim]"
            )
            _print_generated_root_files(files)


if __name__ == "__main__":
    app()
