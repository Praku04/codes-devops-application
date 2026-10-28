"""
Project-specific rules derived from the plan, for every target IDE.

The deduction engine asks the LLM for rules, but what comes back varies: sometimes a rich
grounded set, sometimes two generic bullets, sometimes nothing at all. These builders read
the plan's own scaffold, agents, and skills and produce rules that name *this* project's
domain slices and artifacts, so the floor is identical on Claude Code, Cursor, and
Windsurf. Anything the engine or the user authored is kept; these only fill the gaps.

Because they are derived rather than stored, **existing** projects gain them on their next
render — the same rendered-not-stored approach as `.gitignore` and `CLAUDE.md`.

Only the file dialect differs per IDE:

- **Cursor** — `.cursor/rules/*.mdc`, apply mode in `alwaysApply` / `globs` frontmatter.
- **Windsurf** — `.windsurf/rules/*.md`, apply mode in a `trigger` frontmatter field.
- **Claude Code** — `.claude/rules/*.md`, plain markdown with **no frontmatter dialect at
  all**. Its rules are modular files that `CLAUDE.md` pulls in with `@path`, so apply mode
  has to be expressed two other ways: always-on rules are imported (in context every
  session), and scoped rules state their globs in their own header, which the renderer
  inserts. That header is the only place Claude will read a scope.

So the bodies here carry no apply mode of their own — each exporter writes it in the form
its IDE understands, and one source of truth beats two that can disagree.
"""

from __future__ import annotations

from app.core.state_machine import Platform
from app.models.schemas import ProjectPlan, RuleSpec

# Claude Code rule files are plain markdown — no `.mdc` frontmatter dialect.
CLAUDE_RULE_EXT = ".md"
CLAUDE_RULES_DIR = ".claude/rules"

_RULES_DIR = {
    Platform.CLAUDE_CODE: CLAUDE_RULES_DIR,
    Platform.CURSOR: ".cursor/rules",
    Platform.WINDSURF: ".windsurf/rules",
    Platform.GITHUB_COPILOT: ".github/instructions",
}

_RULE_EXT = {
    Platform.CLAUDE_CODE: CLAUDE_RULE_EXT,
    Platform.CURSOR: ".mdc",
    Platform.WINDSURF: ".md",
    Platform.GITHUB_COPILOT: ".instructions.md",
}


def rules_dir(platform: Platform | None) -> str:
    """Folder this platform's rules are exported to, with a trailing slash."""
    return _RULES_DIR.get(platform or Platform.CLAUDE_CODE, CLAUDE_RULES_DIR) + "/"


def rule_path(rule_name: str, platform: Platform | None) -> str:
    """Where one rule lands in the export for this platform."""
    key = platform or Platform.CLAUDE_CODE
    return f"{_RULES_DIR.get(key, CLAUDE_RULES_DIR)}/{rule_name}{_RULE_EXT.get(key, '.md')}"


def claude_rule_path(rule_name: str) -> str:
    """Where one rule lands in a Claude Code export."""
    return rule_path(rule_name, Platform.CLAUDE_CODE)


def _artifact_dir(platform: Platform | None, kind: str) -> str:
    """`.claude/agents/`, `.cursor/skills/`, … — quoted inside rule bodies."""
    root = {
        Platform.CURSOR: ".cursor",
        Platform.WINDSURF: ".windsurf",
        Platform.GITHUB_COPILOT: ".github",
    }.get(platform or Platform.CLAUDE_CODE, ".claude")
    return f"{root}/{kind}/"


def _scaffold_paths(plan: ProjectPlan) -> list[str]:
    return [
        (spec.path or "").replace("\\", "/").lstrip("/") for spec in (plan.source_tree or [])
    ]


def _has(plan: ProjectPlan, prefix: str) -> bool:
    return any(p.startswith(prefix) for p in _scaffold_paths(plan))


def _frontend_kind(plan: ProjectPlan) -> str:
    """Which frontend flavour was scaffolded, so its rule names the right files."""
    paths = _scaffold_paths(plan)
    if any(p.startswith("frontend/templates/") for p in paths):
        return "jinja"
    if any(p.endswith(".component.ts") for p in paths):
        return "angular"
    if any(p.startswith("frontend/") and p.endswith((".tsx", ".ts")) for p in paths):
        return "next"
    return "none"


def _domain_modules(plan: ProjectPlan) -> list[str]:
    """
    Domain slices this project actually has, read off `backend/services/*_service.py`.

    Naming them in the rule is what makes it project-specific instead of boilerplate —
    the IDE sees `payments`, `ledger`, not "your domain".
    """
    mods: list[str] = []
    for path in _scaffold_paths(plan):
        if path.startswith("backend/services/") and path.endswith("_service.py"):
            mod = path.rsplit("/", 1)[-1][: -len("_service.py")]
            if mod and mod not in mods:
                mods.append(mod)
    return mods


def _summary_line(plan: ProjectPlan) -> str:
    return " ".join((plan.summary or "").split())[:220] or plan.project_name


def _architecture_rule(plan: ProjectPlan) -> RuleSpec:
    mods = _domain_modules(plan)
    mod_line = (
        f"Domain slices in this project: {', '.join(f'`{m}`' for m in mods)}. "
        "One service + one repository per slice; do not merge them."
        if mods
        else "Add one service + one repository per domain slice."
    )
    body = [
        f"# Architecture — {plan.project_name}",
        "",
        f"> {_summary_line(plan)}",
        "",
        "## Layer boundaries",
        "- `backend/api/routes/*` — validate with a Pydantic schema, call one service, return a DTO.",
        "  No SQL, no business branching, no cross-service orchestration in a route.",
        "- `backend/services/*` — all business rules and invariants. Services may call other",
        "  services and repositories; they never touch a DB session directly.",
        "- `backend/repositories/*` — the only layer that issues queries. One repository per",
        "  aggregate; return domain objects or DTOs, never raw rows.",
        "- `backend/models/` (ORM) and `backend/schemas/` (API DTOs) stay separate. Never return",
        "  an ORM entity from a route.",
        "- `backend/core/` — settings, security, session factory. Read config through",
        "  `backend/core/config.py`; never call `os.environ` inline or hardcode a secret.",
        "",
        "## This project",
        f"- {mod_line}",
        "- `main.py` only boots the app. Wiring belongs in `backend/api/app.py`.",
    ]
    if _has(plan, "backend/core/redis.py"):
        body.append("- Redis is for cache and queues only — it is never the source of truth.")
    body += [
        "",
        "## Refuse",
        "Do not add a route that queries the database, a repository that enforces a business",
        "rule, or a service that builds an HTTP response. If a change seems to need it, the",
        "layer split is wrong — say so instead of working around it.",
    ]
    return RuleSpec(
        name="architecture",
        description=f"Layer boundaries for {plan.project_name}: routes → services → repositories.",
        body="\n".join(body),
        always_apply=True,
        globs=[],
    )


def _secure_coding_rule(plan: ProjectPlan) -> RuleSpec:
    body = [
        "# Secure coding — VAPT gate",
        "",
        "Every change is reviewed against this before it counts as done.",
        "",
        "## Never ship",
        "- SQL/NoSQL assembled by string concatenation or f-string. Parameterize, always.",
        "- Unescaped user content rendered into HTML/JS (XSS).",
        "- Untrusted input interpolated into a log line (log injection / forged entries).",
        "- User input reaching a shell, filesystem path, regex, or outbound URL",
        "  (command injection, path traversal, ReDoS, SSRF).",
        "- A read or write of someone else's record without an object-level ownership check (IDOR).",
        "- Mass assignment: never build an ORM entity straight from request JSON.",
        "- Secrets in code, tests, fixtures, logs, or error messages.",
        "",
        "## Always",
        "- Validate at the trust boundary with a Pydantic schema — allow-list fields, bound sizes.",
        "- Authenticate *and* authorize. Being logged in is not permission to touch a row.",
        "- Return generic errors outward; keep stack traces and SQL in server logs.",
        "- Hash passwords with bcrypt/argon2. Never encrypt, never store reversibly.",
    ]
    if _has(plan, "backend/core/security.py"):
        body.append(
            "- Use the helpers in `backend/core/security.py` — do not re-implement token or "
            "hash logic per feature."
        )
    body += [
        "",
        "Fix or explicitly waive every High/Critical finding before pen-test handoff.",
    ]
    return RuleSpec(
        name="secure-coding-vapt",
        description=(
            "Always-on VAPT gate: no SQLi, XSS, log injection, IDOR, SSRF, or secret leakage."
        ),
        body="\n".join(body),
        always_apply=True,
        globs=[],
    )
def _observability_rule(plan: ProjectPlan) -> RuleSpec:
    body = [
        "# Observability standards",
        "",
        "Every production-relevant change must remain observable and diagnosable.",
        "",
        "## Always",
        "- Use structured logs with useful operational context and correlation identifiers.",
        "- Capture appropriate request, service, dependency, and error telemetry.",
        "- Track latency, failures, and important project-specific health signals.",
        "- Keep detailed logs available for deeper troubleshooting.",
        "- Provide a visual observability dashboard as the first-level monitoring experience.",
        "- Make dashboard signals traceable to correlation/trace context and detailed logs.",
        "- Never log passwords, access tokens, API keys, authorization headers, or secrets.",
        "- Avoid unnecessary sensitive or PII content in telemetry.",
    ]

    if any(
        key in f"{a.name} {a.description}".lower()
        for a in plan.agents
        for key in ("llm", "rag", "agent", "retrieval")
    ):
        body += [
            "",
            "## AI / LLM observability",
            "- Track applicable model latency, token usage, agent/tool execution, retrieval",
            "  performance, grounding failures, and guardrail events.",
        ]

    return RuleSpec(
        name="observability-standards",
        description=(
            "Always-on project observability standards for telemetry, diagnostics, "
            "safe logging, and visual dashboard monitoring."
        ),
        body="\n".join(body),
        always_apply=True,
        globs=[],
    )


def _guardrails_rule(plan: ProjectPlan) -> RuleSpec:
    body = [
        "# Guardrails standards",
        "",
        "Apply guardrails at the application's trust boundaries before data or actions",
        "reach protected services, integrations, models, or persistence.",
        "",
        "## Always",
        "- Validate and normalize external input using explicit schemas and allow-lists.",
        "- Enforce authentication and authorization before protected reads, writes, or actions.",
        "- Protect sensitive data in requests, responses, storage, telemetry, and errors.",
        "- Validate external integration inputs and outputs before trusting them.",
        "- Apply safe failure handling and appropriate abuse/rate/resource controls.",
        "- Keep guardrail decisions testable and auditable where appropriate.",
    ]

    if any(
        key in f"{a.name} {a.description}".lower()
        for a in plan.agents
        for key in ("llm", "rag", "retrieval")
    ):
        body += [
            "",
            "## AI / LLM guardrails",
            "- Protect against prompt injection and unsafe tool or action execution.",
            "- Validate model inputs and outputs and protect sensitive/PII data.",
            "- Apply grounding, retrieval-source, and hallucination controls where applicable.",
            "- Record guardrail decisions without exposing sensitive prompt or user content.",
        ]

    return RuleSpec(
        name="guardrails-standards",
        description=(
            "Always-on project guardrails for input, output, access, data, integration, "
            "and AI safety controls where applicable."
        ),
        body="\n".join(body),
        always_apply=True,
        globs=[],
    )



def _testing_rule(plan: ProjectPlan) -> RuleSpec:
    globs = ["backend/tests/**/*.py"]
    if _has(plan, "frontend/tests/"):
        globs.append("frontend/tests/**")
    body = [
        "# Testing",
        "",
        "- Tests ship in the same change as the code. A phase is not done until its",
        "  **Test & acceptance** items in `WORKBREAKDOWN.md` pass.",
        "- Unit-test services with repositories faked; the point is the business rule.",
        "- Integration-test routes through the app factory, not by calling handlers directly.",
        "- Name tests for the behaviour, not the method: `test_refund_rejected_after_settlement`.",
        "- Every bug fix starts with a test that fails for that bug.",
        "- Assert on outcomes, not call counts. Never assert against a hardcoded timestamp",
        "  or a real network call.",
    ]
    return RuleSpec(
        name="testing",
        description="Test placement, naming, and the acceptance gate for each phase.",
        body="\n".join(body),
        always_apply=False,
        globs=globs,
    )


def _frontend_rule(plan: ProjectPlan) -> RuleSpec | None:
    kind = _frontend_kind(plan)
    if kind == "none":
        return None

    if kind == "jinja":
        globs = ["frontend/templates/**", "frontend/static/**"]
        lines = [
            "- Pages extend `frontend/templates/base.html`; shared chrome lives in partials.",
            "- Templates render data the route already prepared — no business logic in Jinja.",
            "- Autoescaping stays on. Never `| safe` user-supplied content.",
            "- Browser JS in `frontend/static/js/` calls backend routes; keep DOM code out of templates.",
        ]
    elif kind == "angular":
        globs = ["frontend/src/**"]
        lines = [
            "- Components render state; HTTP calls belong in a service under `services/`.",
            "- Type every API response — no `any` at the boundary.",
            "- Unsubscribe or use the `async` pipe; leaked subscriptions are a defect.",
        ]
    else:
        globs = ["frontend/**"]
        lines = [
            "- `app/` holds routes, `features/` domain slices, `components/` presentational UI.",
            "- Data loading goes through `hooks/` → `services/` → `lib/api-client.ts`.",
            "  Never `fetch` inline in a component.",
            "- Types in `types/` mirror backend schemas — update both together.",
            "- Presentational components take props and render; they do not fetch.",
        ]

    body = [
        "# Frontend",
        "",
        *lines,
        "",
        "Never render untrusted HTML, and never put a secret or API key in frontend code —",
        "anything shipped to the browser is public.",
    ]
    return RuleSpec(
        name="frontend",
        description=f"UI conventions for the {kind} frontend in this project.",
        body="\n".join(body),
        always_apply=False,
        globs=globs,
    )


def _workflow_rule(plan: ProjectPlan, platform: Platform | None) -> RuleSpec:
    agents = [a.name for a in plan.agents]
    skills = [s.name for s in plan.skills]
    body = [
        "# Workflow",
        "",
        "- Work one phase of `WORKBREAKDOWN.md` at a time. Finish its acceptance gate",
        "  before starting the next.",
        "- Scaffold files ship as a `Role:` docstring only. Replace one with real code when",
        "  its phase calls for it — do not bulk-fill the tree.",
        "- Keep a file's `Role:` line accurate if its responsibility changes.",
    ]
    if agents:
        shown = ", ".join(f"`{n}`" for n in agents[:6])
        body.append(
            f"- Delegate specialist work to the agents in "
            f"`{_artifact_dir(platform, 'agents')}` ({shown}) rather than doing it inline."
        )
    if skills:
        shown = ", ".join(f"`{n}`" for n in skills[:6])
        body.append(
            f"- Skills in `{_artifact_dir(platform, 'skills')}` ({shown}) auto-invoke from "
            "their descriptions — prefer them over ad-hoc instructions."
        )
    body += [
        "- Ask before adding a dependency, changing the DB schema, or altering an API contract.",
    ]
    return RuleSpec(
        name="workflow",
        description="How to work through phases and when to delegate to agents and skills.",
        body="\n".join(body),
        always_apply=True,
        globs=[],
    )


def build_project_rules(plan: ProjectPlan, platform: Platform | None = None) -> list[RuleSpec]:
    """
    Project-specific rules for this plan.

    Derived from the plan's own scaffold, agents, and skills, so two different projects get
    different rules — the point a generic set misses. `platform` only affects the IDE folder
    names quoted inside the bodies; the apply mode is left to each exporter's dialect.
    """
    rules = [
        _architecture_rule(plan),
        _secure_coding_rule(plan),
        _observability_rule(plan),
        _guardrails_rule(plan),
        _workflow_rule(plan, platform),
    ]
    frontend = _frontend_rule(plan)
    if frontend:
        rules.append(frontend)
    rules.append(_testing_rule(plan))
    return rules


def ensure_project_rules(plan: ProjectPlan, platform: Platform | None = None) -> list[RuleSpec]:
    """
    Rules to export, keeping any the engine or the user authored.

    A plan with no rules gets the full derived set — that covers existing Claude projects
    (which were stripped of rules) and any plan the LLM returned none for. A plan that
    already carries rules keeps them and only gains the **always-on** ones it is missing, so
    a user who deleted a scoped rule does not see it return, while every project on every
    IDE still has the architecture, security, observability, guardrails, and workflow floor.
    """
    existing = list(plan.rules or [])
    if not existing:
        return build_project_rules(plan, platform)

    have = {r.name for r in existing}
    # Only the always-on guarantees are backfilled; scoped rules are the user's call.
    for rule in build_project_rules(plan, platform):
        if rule.always_apply and rule.name not in have:
            existing.append(rule)
    return existing


def build_claude_rules(plan: ProjectPlan) -> list[RuleSpec]:
    """Claude Code flavour of `build_project_rules`."""
    return build_project_rules(plan, Platform.CLAUDE_CODE)


def ensure_claude_rules(plan: ProjectPlan) -> list[RuleSpec]:
    """Claude Code flavour of `ensure_project_rules`."""
    return ensure_project_rules(plan, Platform.CLAUDE_CODE)


def claude_rule_import_lines(rules: list[RuleSpec]) -> tuple[list[str], list[str]]:
    """
    Split rules into CLAUDE.md `@`-imports and on-demand references.

    Always-on rules are imported so Claude has them every session. Glob-scoped rules are
    listed with their globs instead — importing them all would put every rule in context on
    every prompt, which is the cost `CLAUDE.md` is deliberately kept small to avoid.
    """
    always: list[str] = []
    scoped: list[str] = []
    for rule in rules:
        path = claude_rule_path(rule.name)
        if rule.always_apply or not rule.globs:
            always.append(f"@{path}")
        else:
            scoped.append(f"- `{path}` — when editing `{'`, `'.join(rule.globs)}`")
    return always, scoped
