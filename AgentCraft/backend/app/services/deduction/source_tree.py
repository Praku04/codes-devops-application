"""Analyze a project brief and build a modular application source_tree.

Preferred shapes (examples — adapted to the brief, not copy-pasted blindly):

Backend (FastAPI / Pydantic / SQLAlchemy|SQLModel / Postgres / Redis):
  backend/api/routes/, dependencies/, services/, repositories/,
  models/, schemas/, core/, utils/, tests/

Frontend (Next.js / TypeScript / component-driven + API clients):
  frontend/app/, components/, features/, hooks/, lib/,
  services/, types/, tests/

File contents are docstring/role comments ONLY — no executable code.
"""

from __future__ import annotations

import re
from typing import Any

from app.models.schemas import ProjectBrief, SourceFileSpec


def _slug(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "_", (name or "app").lower()).strip("_")
    return cleaned[:40] or "app"


def _hint(brief: ProjectBrief) -> str:
    parts = [
        brief.problem_statement.strip(),
        brief.tech_stack.strip(),
        ", ".join(brief.domains),
        brief.constraints.strip(),
        ", ".join(brief.integrations),
    ]
    return " | ".join(p for p in parts if p)[:1200]


def _detect_domains(text: str) -> set[str]:
    t = text.lower()
    flags: set[str] = set()
    checks = {
        "payments": ("payment", "ledger", "refund", "webhook", "merchant", "psp", "settlement"),
        "fleet": ("fleet", "vehicle", "telematics", "gps", "driver", "route"),
        "clinic": ("clinic", "patient", "fhir", "hipaa", "ehr", "appointment", "provider"),
        "auth": ("auth", "oauth", "sso", "jwt", "tenant", "rbac"),
        "cache": ("redis", "cache", "queue", "celery", "rq", "bull"),
        "db": ("postgres", "postgresql", "sqlalchemy", "sqlmodel", "sqlite", "mongo"),
    }
    for key, words in checks.items():
        if any(w in t for w in words):
            flags.add(key)
    if not flags & {"payments", "fleet", "clinic"}:
        flags.add("domain")
    flags.update({"auth", "db"})
    return flags


def _detect_stack(text: str) -> dict[str, str]:
    """Infer backend/frontend flavors from brief + tech_stack."""
    t = text.lower()
    backend = "fastapi"
    if "django" in t:
        backend = "django"
    elif "flask" in t:
        backend = "flask"
    elif ("nestjs" in t or "express" in t) or ("node" in t and "api" in t):
        backend = "node"

    frontend = "none"
    if any(w in t for w in ("next.js", "nextjs", "next ")):
        frontend = "next"
    elif "angular" in t:
        frontend = "angular"
    elif "vue" in t or "nuxt" in t:
        frontend = "vue"
    elif any(w in t for w in ("react", "frontend", "spa", "dashboard", "ui ", "web app", "typescript")):
        frontend = "next"
    elif any(w in t for w in ("jinja", "jinja2", "template", "browser spa", "web shell")):
        frontend = "jinja"

    if frontend == "none" and any(
        w in t
        for w in (
            "dashboard",
            "portal",
            "clinic",
            "fleet",
            "merchant",
            "patient",
            "trainee",
            "trainer",
            "learning",
            "course",
            "quiz",
            "tutor",
            "ui/",
            "ux",
            "screen",
            "page",
        )
    ):
        frontend = "next"

    orm = "sqlmodel" if "sqlmodel" in t else "sqlalchemy"
    cache = "redis" if any(w in t for w in ("redis", "cache", "queue")) else "none"
    return {"backend": backend, "frontend": frontend, "orm": orm, "cache": cache}


def _role(purpose: str) -> str:
    return " ".join((purpose or "").split())


def _doc_only(path: str, purpose: str) -> SourceFileSpec:
    """File content is ONLY a role docstring/comment — no executable code."""
    role = _role(purpose) or f"Scaffold for `{path}`."
    p = path.replace("\\", "/").lstrip("/")
    lower = p.lower()

    if lower.endswith(".py"):
        content = f'"""{p}\n\nRole: {role}\n"""\n'
    elif lower.endswith((".ts", ".tsx", ".js", ".jsx")):
        content = f"/**\n * {p}\n * Role: {role}\n */\n"
    elif lower.endswith(".json"):
        content = "{}\n"
    elif lower.endswith((".md", ".markdown")):
        content = f"# {p}\n\nRole: {role}\n"
    else:
        content = f"# {p}\n# Role: {role}\n"

    return SourceFileSpec(path=p, purpose=role, content=content)


def docstring_only_content(path: str, purpose: str) -> str:
    """Public helper: Role docstring/comment body for a scaffold path."""
    return _doc_only(path, purpose).content or ""


# Root-level files every workspace scaffold must include (export + Files tab).
_ROOT_SCAFFOLD: tuple[tuple[str, str], ...] = (
    (
        "requirements.txt",
        "Python dependencies for FastAPI, Pydantic, ORM, Postgres driver, and optional Redis.",
    ),
    (
        ".env.example",
        "Documented environment variables. Copy to `.env` and fill in locally; never commit real secrets.",
    ),
    (
        ".env",
        "Local environment file (gitignored). Copy from `.env.example` and set secrets for this machine.",
    ),
)


def ensure_root_scaffold_files(files: list[SourceFileSpec]) -> list[SourceFileSpec]:
    """Ensure requirements.txt and env templates exist in every source_tree."""
    by_path = {f.path.replace("\\", "/").lstrip("/"): f for f in files}
    for path, purpose in _ROOT_SCAFFOLD:
        if path not in by_path:
            by_path[path] = _doc_only(path, purpose)
    return list(by_path.values())


def source_tree_needs_root_scaffold(files: list[SourceFileSpec] | None) -> bool:
    if not files:
        return True
    paths = {f.path.replace("\\", "/").lstrip("/") for f in files}
    return any(path not in paths for path, _ in _ROOT_SCAFFOLD)


def ensure_package_inits(files: list[SourceFileSpec]) -> list[SourceFileSpec]:
    files = ensure_root_scaffold_files(files)
    by_path = {f.path.replace("\\", "/").lstrip("/"): f for f in files}
    dirs: set[str] = set()
    for path in list(by_path):
        if not path.endswith(".py"):
            continue
        parts = path.split("/")
        for i in range(1, len(parts)):
            dirs.add("/".join(parts[:i]))
    for d in sorted(dirs):
        init = f"{d}/__init__.py"
        if init not in by_path:
            by_path[init] = _doc_only(
                init,
                f"Package marker for `{d}/` so imports stay modular and testable.",
            )
    if "main.py" not in by_path:
        by_path["main.py"] = _doc_only(
            "main.py",
            "Process entrypoint. Boots settings and starts the API server; no business logic here.",
        )
    # Force docstring-only content (strip any accidental code)
    return [_doc_only(path, by_path[path].purpose) for path in sorted(by_path.keys())]


def is_legacy_source_tree(files: list[SourceFileSpec] | None) -> bool:
    """Old flat `app/` trees without `backend/` should be upgraded on export."""
    if not files:
        return True
    paths = [f.path.replace("\\", "/").lstrip("/") for f in files]
    has_backend = any(p.startswith("backend/") for p in paths)
    has_legacy = any(p == "app" or p.startswith("app/") for p in paths)
    return has_legacy and not has_backend


def _content_has_executable_code(path: str, content: str) -> bool:
    """True when scaffold still contains imports/classes/functions beyond a Role docstring."""
    text = (content or "").strip()
    if not text:
        return False
    lower = path.replace("\\", "/").lower()
    # JSON scaffold may be "{}" — ok
    if lower.endswith(".json"):
        return False
    # Strip a leading docstring / block comment, then see if anything remains
    body = text
    if lower.endswith(".py"):
        if body.startswith('"""') or body.startswith("'''"):
            q = body[:3]
            end = body.find(q, 3)
            if end != -1:
                body = body[end + 3 :].strip()
        # leftover code signals pollution
        if not body:
            return False
        return any(
            tok in body
            for tok in (
                "import ",
                "from ",
                "class ",
                "def ",
                "raise ",
                "return ",
                "=",
            )
        )
    if lower.endswith((".ts", ".tsx", ".js", ".jsx")):
        if body.startswith("/**"):
            end = body.find("*/")
            if end != -1:
                body = body[end + 2 :].strip()
        if not body:
            return False
        return any(
            tok in body
            for tok in (
                "import ",
                "export ",
                "function ",
                "const ",
                "class ",
                "=>",
            )
        )
    # Other text files: Role comment only is fine; treat non-comment lines as code-ish
    lines = [ln for ln in body.splitlines() if ln.strip()]
    if not lines:
        return False
    if all(ln.lstrip().startswith("#") or ln.lstrip().startswith(">") for ln in lines):
        return False
    if lower.endswith((".md", ".markdown")) and all(
        ln.startswith("#") or ln.startswith(">") or ln.startswith("Role:") for ln in lines
    ):
        return False
    # If more than a short role header, consider it polluted for scaffold purposes
    return len(lines) > 4


def source_tree_needs_doc_sanitize(files: list[SourceFileSpec] | None) -> bool:
    if not files:
        return False
    return any(_content_has_executable_code(f.path, f.content or "") for f in files)


def sanitize_source_tree_docs_only(files: list[SourceFileSpec] | None) -> list[SourceFileSpec]:
    """Rewrite every scaffold file to Role docstring/comment only (no code)."""
    if not files:
        return []
    return ensure_package_inits(list(files))


def normalize_source_meta(raw: list[Any] | None) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").replace("\\", "/").lstrip("/")
        purpose = str(item.get("purpose") or "").strip()
        if not path or path in seen or ".." in path.split("/"):
            continue
        seen.add(path)
        out.append({"path": path, "purpose": purpose or f"Module for {path}"})
    return out


def _domain_names(domains: set[str], project_name: str) -> list[str]:
    names: list[str] = []
    if "payments" in domains:
        names.append("payments")
    if "fleet" in domains:
        names.append("fleet")
    if "clinic" in domains:
        names.append("clinic")
    if "domain" in domains and not names:
        names.append(_slug(project_name.replace("-", "_")))
    if "auth" in domains and "auth" not in names:
        names.insert(0, "auth")
    return names or ["domain"]


def _build_backend(
    *,
    project_name: str,
    domains: set[str],
    stack: dict[str, str],
) -> list[SourceFileSpec]:
    files: list[SourceFileSpec] = []
    orm = stack["orm"]
    cache = stack["cache"]
    domain_mods = _domain_names(domains, project_name)

    files.append(
        _doc_only(
            "main.py",
            "Thin process entrypoint. Create the FastAPI app and run the server — no business logic.",
        )
    )
    files.append(_doc_only("backend/__init__.py", "Backend root package (FastAPI + Pydantic + ORM layers)."))
    files.append(_doc_only("backend/api/__init__.py", "HTTP layer: app factory, routes, and FastAPI dependencies."))
    files.append(_doc_only("backend/api/routes/__init__.py", "Versioned / feature route modules; handlers stay thin."))
    files.append(
        _doc_only(
            "backend/api/dependencies/__init__.py",
            "Shared FastAPI Depends() providers (DB session, current user, redis).",
        )
    )
    files.append(_doc_only("backend/services/__init__.py", "Domain services — business rules live here, not in routes."))
    files.append(
        _doc_only(
            "backend/repositories/__init__.py",
            "Persistence adapters over SQLAlchemy/SQLModel sessions.",
        )
    )
    files.append(_doc_only("backend/models/__init__.py", f"ORM entities ({orm}) mapped to PostgreSQL tables."))
    files.append(_doc_only("backend/schemas/__init__.py", "Pydantic request/response DTOs at the API boundary."))
    files.append(_doc_only("backend/core/__init__.py", "Settings, security helpers, and cross-cutting config."))
    files.append(_doc_only("backend/utils/__init__.py", "Pure helpers (ids, time, hashing) with no I/O."))
    files.append(_doc_only("backend/tests/__init__.py", "Backend pytest package."))

    files.append(
        _doc_only(
            "backend/core/config.py",
            "Typed settings from env/.env (DB URL, Redis URL, secrets). Single source of truth for config.",
        )
    )
    files.append(
        _doc_only(
            "backend/core/security.py",
            "AuthN/AuthZ helpers (JWT, password hashing, tenant extraction). Call before mutating writes.",
        )
    )
    files.append(
        _doc_only(
            "backend/core/database.py",
            f"Engine/session factory for {orm} against PostgreSQL. Repositories depend on this, not routes.",
        )
    )
    if cache == "redis":
        files.append(
            _doc_only(
                "backend/core/redis.py",
                "Redis client for cache and lightweight queue/pub-sub. Keep connection lifecycle here.",
            )
        )

    files.append(
        _doc_only(
            "backend/api/app.py",
            "FastAPI application factory. Registers middleware, exception handlers, and route modules.",
        )
    )
    files.append(
        _doc_only(
            "backend/api/dependencies/db.py",
            "FastAPI dependency that yields a DB session per request.",
        )
    )
    files.append(
        _doc_only(
            "backend/api/dependencies/auth.py",
            "Resolve current user / tenant from Authorization header for protected routes.",
        )
    )
    files.append(
        _doc_only(
            "backend/api/routes/health.py",
            "Liveness/readiness endpoints used by deploy probes and smoke tests.",
        )
    )
    files.append(
        _doc_only(
            "backend/utils/ids.py",
            "ID helpers (UUID/ulid). Keep generation consistent across services and repositories.",
        )
    )
    files.append(
        _doc_only(
            "backend/tests/test_health.py",
            "Smoke test that the app factory boots and /health is wired.",
        )
    )

    for mod in domain_mods:
        title = mod.replace("_", " ")
        files.append(
            _doc_only(
                f"backend/api/routes/{mod}.py",
                f"HTTP routes for {title}. Validate with Pydantic schemas; call services only — no SQL here.",
            )
        )
        files.append(
            _doc_only(
                f"backend/services/{mod}_service.py",
                f"Business rules for {title}. Orchestrates repositories, cache, and domain invariants.",
            )
        )
        files.append(
            _doc_only(
                f"backend/repositories/{mod}_repository.py",
                f"Data access for {title}. Isolates {orm} queries so services stay unit-testable.",
            )
        )
        files.append(
            _doc_only(
                f"backend/models/{mod}.py",
                f"ORM table mapping for {title} ({orm} / PostgreSQL).",
            )
        )
        files.append(
            _doc_only(
                f"backend/schemas/{mod}.py",
                f"Pydantic DTOs for {title} create/read/update at the API edge.",
            )
        )

    return files


def _build_jinja_frontend(
    *,
    project_name: str,
    domains: set[str],
) -> list[SourceFileSpec]:
    """Jinja2 + static/JS UI scaffold (FastAPI-served web shell)."""
    domain_mods = [m for m in _domain_names(domains, project_name) if m != "auth"]
    feature = domain_mods[0] if domain_mods else "home"
    return [
        _doc_only(
            "frontend/README.md",
            "Web UI served via FastAPI Jinja2 templates + static assets; pairs with backend API routes.",
        ),
        _doc_only(
            "frontend/templates/base.html",
            "Base layout (nav, auth chrome, flash messages). Extend in feature templates.",
        ),
        _doc_only(
            f"frontend/templates/{feature}/index.html",
            f"Jinja page for `{feature}` — compose partials; fetch data via backend routes or HTMX.",
        ),
        _doc_only(
            "frontend/templates/partials/nav.html",
            "Shared navigation partial included from base.html.",
        ),
        _doc_only(
            "frontend/static/css/app.css",
            "Global styles for the web shell and feature pages.",
        ),
        _doc_only(
            "frontend/static/js/app.js",
            "Client-side helpers (quiz UI, module study, voice/video flows) calling backend APIs.",
        ),
        _doc_only(
            f"frontend/static/js/{feature}.js",
            f"Feature-specific UI logic for `{feature}` pages.",
        ),
        _doc_only(
            "backend/api/routes/ui.py",
            "FastAPI routes that render Jinja templates and serve the browser SPA shell.",
        ),
    ]


def _build_frontend(
    *,
    project_name: str,
    domains: set[str],
    stack: dict[str, str],
) -> list[SourceFileSpec]:
    if stack["frontend"] == "none":
        return []
    if stack["frontend"] == "jinja":
        return _build_jinja_frontend(project_name=project_name, domains=domains)

    files: list[SourceFileSpec] = []
    domain_mods = [m for m in _domain_names(domains, project_name) if m != "auth"]
    feature = domain_mods[0] if domain_mods else "home"

    files.append(
        _doc_only(
            "frontend/README.md",
            "Frontend workspace notes for the Next.js + TypeScript UI that talks to the FastAPI backend.",
        )
    )
    files.append(
        _doc_only(
            "frontend/app/layout.tsx",
            "Root layout. Wraps pages with providers; keep global chrome here, not feature logic.",
        )
    )
    files.append(
        _doc_only(
            "frontend/app/page.tsx",
            "Home route. Compose feature modules; avoid inline API calls — use hooks/services.",
        )
    )
    files.append(
        _doc_only(
            f"frontend/app/{feature}/page.tsx",
            f"Route for the `{feature}` feature. Renders feature components and wires data hooks.",
        )
    )
    files.append(
        _doc_only(
            "frontend/components/ui/button.tsx",
            "Shared presentational button. Keep styling tokens here; no domain fetches.",
        )
    )
    files.append(
        _doc_only(
            f"frontend/features/{feature}/{feature}-panel.tsx",
            f"Feature UI for `{feature}`. Owns layout of this domain slice; calls hooks for data.",
        )
    )
    files.append(
        _doc_only(
            f"frontend/hooks/use-{feature}.ts",
            f"React hook for `{feature}` data loading/mutations via frontend services.",
        )
    )
    files.append(
        _doc_only(
            "frontend/lib/api-client.ts",
            "Shared fetch wrapper (base URL, auth header, error mapping) used by all services.",
        )
    )
    files.append(
        _doc_only(
            f"frontend/services/{feature}-api.ts",
            f"Typed API client for `{feature}` backend routes under `/api/v1`.",
        )
    )
    files.append(
        _doc_only(
            f"frontend/types/{feature}.ts",
            f"Shared TypeScript types for `{feature}` DTOs mirrored from backend schemas.",
        )
    )
    files.append(
        _doc_only(
            f"frontend/tests/{feature}.test.ts",
            f"Unit/smoke tests for `{feature}` frontend helpers and types.",
        )
    )
    files.append(
        _doc_only(
            "frontend/package.json",
            "Frontend package manifest (Next.js + TypeScript).",
        )
    )
    return files

def _build_mandatory_operational_files(
    *,
    project_name: str,
    domains: set[str],
    stack: dict[str, str],
) -> list[SourceFileSpec]:
    """
    Mandatory project-specific observability and guardrail scaffold.

    These are implementation targets for the generated project. Keep them
    stack-aware and docstring-only; the coding assistant implements the
    executable bodies later.
    """
    files: list[SourceFileSpec] = []

    backend = stack["backend"]
    frontend = stack["frontend"]

    # ── Observability ──────────────────────────────────────────────────────
    if backend in {"fastapi", "django", "flask"}:
        files.append(
            _doc_only(
                "backend/core/observability.py",
                "Configure project telemetry, structured logging, tracing, metrics, and correlation IDs.",
            )
        )
        files.append(
            _doc_only(
                "backend/api/routes/observability.py",
                "Expose authorized observability data for dashboard health, metrics, traces, and errors.",
            )
        )
    elif backend == "node":
        files.append(
            _doc_only(
                "backend/core/observability.ts",
                "Configure project telemetry, structured logging, tracing, metrics, and correlation IDs.",
            )
        )
        files.append(
            _doc_only(
                "backend/api/routes/observability.ts",
                "Expose authorized observability data for dashboard health, metrics, traces, and errors.",
            )
        )

    # Visual dashboard is mandatory. Adapt its shape to the detected UI stack.
    if frontend == "next":
        files.append(
            _doc_only(
                "frontend/app/observability/page.tsx",
                "Visual monitoring dashboard for health, traffic, latency, errors, traces, and project signals.",
            )
        )
        files.append(
            _doc_only(
                "frontend/features/observability/observability-dashboard.tsx",
                "Dashboard components for project-specific metrics, traces, errors, and integration health.",
            )
        )
    elif frontend == "angular":
        files.append(
            _doc_only(
                "frontend/src/app/features/observability/observability-dashboard.component.ts",
                "Visual monitoring dashboard for health, traffic, latency, errors, traces, and project signals.",
            )
        )
    elif frontend == "vue":
        files.append(
            _doc_only(
                "frontend/src/views/ObservabilityDashboard.vue",
                "Visual monitoring dashboard for health, traffic, latency, errors, traces, and project signals.",
            )
        )
    elif frontend == "jinja":
        files.append(
            _doc_only(
                "frontend/templates/observability/index.html",
                "Visual monitoring dashboard for health, traffic, latency, errors, traces, and project signals.",
            )
        )

    # ── Guardrails ─────────────────────────────────────────────────────────
    if backend in {"fastapi", "django", "flask"}:
        files.append(
            _doc_only(
                "backend/core/guardrails.py",
                "Central project guardrails for validation, authorization, data protection, and safe failures.",
            )
        )
    elif backend == "node":
        files.append(
            _doc_only(
                "backend/core/guardrails.ts",
                "Central project guardrails for validation, authorization, data protection, and safe failures.",
            )
        )

    return files

def build_source_tree_from_brief(
    brief: ProjectBrief,
    *,
    project_name: str = "agentcraft-project",
    summary: str = "",
) -> list[SourceFileSpec]:
    """Analyze brief → realistic modular backend (+ frontend when implied)."""
    text = f"{_hint(brief)}\n{summary}\n{project_name}"
    domains = _detect_domains(text)
    stack = _detect_stack(text)
    name = _slug(project_name).replace("_", "-") or "agentcraft-project"

    files = _build_backend(project_name=name, domains=domains, stack=stack)
    files.extend(_build_frontend(project_name=name, domains=domains, stack=stack))
    files.extend(
        _build_mandatory_operational_files(
            project_name=name,
            domains=domains,
            stack=stack,
        )
    )
    return ensure_package_inits(files)


def merge_llm_source_files(
    meta: list[dict[str, str]],
    expanded: list[Any] | None,
    brief: ProjectBrief,
    project_name: str,
    summary: str,
) -> list[SourceFileSpec]:
    by_path: dict[str, SourceFileSpec] = {}
    for m in meta:
        by_path[m["path"]] = _doc_only(m["path"], m["purpose"])

    for item in expanded or []:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").replace("\\", "/").lstrip("/")
        if not path or ".." in path.split("/"):
            continue
        purpose = str(item.get("purpose") or "").strip() or f"Module for `{path}`."
        # Always docstring-only — ignore any executable code the model may return
        by_path[path] = _doc_only(path, purpose)

    if not by_path:
        return build_source_tree_from_brief(brief, project_name=project_name, summary=summary)

    if "main.py" not in by_path:
        by_path["main.py"] = _doc_only(
            "main.py",
            "Process entrypoint. Boots settings and starts the API server; no business logic here.",
        )
        
    text = f"{_hint(brief)}\n{summary}\n{project_name}"
    domains = _detect_domains(text)
    stack = _detect_stack(text)

    for spec in _build_mandatory_operational_files(
        project_name=project_name,
        domains=domains,
        stack=stack,
    ):
        path = spec.path.replace("\\", "/").lstrip("/")
        by_path.setdefault(path, spec)


    return ensure_package_inits(list(by_path.values()))


def _brief_implies_frontend(brief: ProjectBrief, summary: str = "") -> bool:
    parts = [
        brief.problem_statement,
        brief.tech_stack,
        summary,
        " ".join(brief.domains),
        " ".join(brief.integrations),
    ]
    text = " ".join(p for p in parts if p).lower()
    ui_keywords = (
        "frontend",
        "react",
        "next",
        "angular",
        "vue",
        "spa",
        "dashboard",
        "ui ",
        "web app",
        "jinja",
        "template",
        "browser",
        "portal",
        "trainee",
        "trainer",
        "learning",
        "quiz",
        "course",
        "tutor",
        "typescript",
        "javascript",
        "tailwind",
        "screen",
        "page",
        "ux",
    )
    return any(k in text for k in ui_keywords)


def source_tree_needs_frontend(
    files: list[SourceFileSpec] | None,
    brief: ProjectBrief | None,
    summary: str = "",
) -> bool:
    """True when the brief implies a UI but the tree has no frontend/ (or Jinja UI) paths."""
    if not _brief_implies_frontend(brief or ProjectBrief(), summary):
        return False
    paths = {f.path.replace("\\", "/").lstrip("/") for f in (files or []) if f.path}
    has_frontend = any(p.startswith("frontend/") for p in paths)
    has_jinja_ui = any(p.startswith("backend/templates/") or p == "backend/api/routes/ui.py" for p in paths)
    return not (has_frontend or has_jinja_ui)


def ensure_frontend_in_tree(
    files: list[SourceFileSpec] | None,
    brief: ProjectBrief | None,
    *,
    project_name: str,
    summary: str,
) -> list[SourceFileSpec]:
    """Merge frontend scaffold paths when missing from an existing plan."""
    if not source_tree_needs_frontend(files, brief, summary):
        return list(files or [])
    by_path = {f.path.replace("\\", "/").lstrip("/"): f for f in (files or [])}
    fresh = build_source_tree_from_brief(
        brief or ProjectBrief(),
        project_name=project_name,
        summary=summary,
    )
    for spec in fresh:
        p = spec.path.replace("\\", "/").lstrip("/")
        if p.startswith("frontend/") or p in {"backend/api/routes/ui.py"}:
            by_path.setdefault(p, spec)
    return ensure_package_inits(list(by_path.values()))


def ensure_plan_source_tree(
    plan_source: list[SourceFileSpec] | None,
    brief: ProjectBrief | None,
    *,
    project_name: str,
    summary: str,
) -> list[SourceFileSpec]:
    if plan_source and not is_legacy_source_tree(plan_source):
        return sanitize_source_tree_docs_only(plan_source)
    return build_source_tree_from_brief(
        brief or ProjectBrief(),
        project_name=project_name,
        summary=summary,
    )
