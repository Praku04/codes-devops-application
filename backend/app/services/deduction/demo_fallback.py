"""Deterministic detailed demo plan (CLI --demo / offline tests)."""

from __future__ import annotations

from app.core.state_machine import Platform
from app.models.schemas import ProjectBrief, ProjectPlan
from app.services.deduction.solution_design import (
    fallback_solution_design,
    fallback_solution_design_views,
    is_solution_design_complete,
)
from app.services.deduction.source_tree import build_source_tree_from_brief
from app.services.deduction.work_breakdown import fallback_work_breakdown


def _clip_lines(text: str, max_lines: int = 49) -> str:
    raw = [ln.rstrip() for ln in text.strip().splitlines()]
    lines: list[str] = []
    blank_run = 0
    for ln in raw:
        if not ln.strip():
            blank_run += 1
            if blank_run > 1:
                continue
            lines.append("")
        else:
            blank_run = 0
            lines.append(ln)
    while lines and not lines[-1]:
        lines.pop()
    if len(lines) <= max_lines:
        return "\n".join(lines)
    return "\n".join(lines[:max_lines])


def _domain_hint(brief: ProjectBrief) -> str:
    parts = [
        brief.problem_statement.strip(),
        brief.tech_stack.strip(),
        ", ".join(brief.domains),
        brief.constraints.strip(),
    ]
    return " | ".join(p for p in parts if p)[:400] or "general software delivery"


def build_demo_plan(brief: ProjectBrief, platform: Platform) -> ProjectPlan:
    hint = _domain_hint(brief)
    name = "agentcraft-project"
    if brief.problem_statement:
        name = "-".join(
            w for w in brief.problem_statement.lower().replace(",", " ").split()[:5] if w.isalnum()
        ) or name

    summary = (
        f"Workspace for: {brief.problem_statement[:280]}"
        if brief.problem_statement
        else "Generated AgentCraft workspace with VAPT-ready agents, skills, and rules."
    )

    backend_prompt = _clip_lines(
        f"""# Backend Architect
You own APIs and data for:
> {hint}

## Goals
- Thin handlers; logic in services.
- `/api/v1` + migrations; schema validation at the edge.

## Workflow
1. Restate change and blast radius.
2. Propose schema/API deltas + compat notes.
3. Add success and failure tests.
4. Call out authZ / tenancy before merge.

## Example
User: "Add refund webhook for tenant X"
→ Design idempotent `POST /api/v1/webhooks/refunds`, HMAC verify, ledger reverse entry, 409 on replay.

## Acceptance
Parameterized queries/ORM only; no secrets in code.
"""
    )
    frontend_prompt = _clip_lines(
        f"""# Frontend Specialist
UI for:
> {hint}

## Goals
- One job per screen; typed forms; accessible errors.
- Never trust client-only auth.

## Example
User: "Merchant can't see refund status"
→ Trace API → typed service → status chip + retry; no tokens in localStorage if httpOnly cookies exist.

## Avoid
User-controlled regex; raw HTML without encoding.
"""
    )
    qa_prompt = _clip_lines(
        f"""# QA Engineer
Quality for:
> {hint}

## Workflow
1. Risk matrix (auth, money, webhooks).
2. Unit + API + negative authZ.
3. Pre-VAPT: injection, IDOR, overlong input.

## Example
Feature: multi-tenant payout
→ Assert tenant A cannot read tenant B payout IDs (IDOR).
"""
    )
    devops_prompt = _clip_lines(
        f"""# DevOps Engineer
CI/CD for:
> {hint}

## Pipeline
Lint → test → CVE → SAST → build → deploy.
Secrets via env/vault; non-root images.

## Example
PR adds ECS task env
→ Block plaintext secrets; require SSM/Secrets Manager refs.
"""
    )
    security_prompt = _clip_lines(
        f"""# Security / VAPT Reviewer
Pre-VAPT gate for:
> {hint}

## Goals
- Block High/Critical findings before merge or VAPT handoff
- SQL/NoSQL injection → parameterized queries / ORM only
- XSS (reflected/stored/DOM) → encode output; never raw user HTML/JS
- Log injection → strip CR/LF; never log raw untrusted strings as lines
- Command injection, path traversal, SSRF, XXE
- IDOR / broken authZ on every object access
- CSRF (cookie apps), mass assignment, insecure deserialization
- ReDoS (no user-controlled regex); secrets/PII never in logs

## Workflow
1. Diff review against the Goals list above.
2. Demand fix or written waiver for High/Critical.
3. Refuse VAPT handoff until clear.

## Example
Route builds SQL with f"…{{user_id}}"
→ Reject; use bound params / ORM filter.
"""
    )

    payload = {
            "project_name": name[:64],
            "summary": summary,
            "agents": [
                {
                    "name": "backend-architect",
                    "description": "Designs APIs and schemas. Use for backend architecture changes.",
                    "system_prompt": backend_prompt,
                    "tools": ["Read", "Write", "Grep", "Glob", "Shell"],
                    "skills": ["api-design", "pre-vapt-secure-coding"],
                },
                {
                    "name": "frontend-specialist",
                    "description": "Implements UI flows. Use for screens and client integration.",
                    "system_prompt": frontend_prompt,
                    "tools": ["Read", "Write", "Grep", "Glob"],
                    "skills": ["ui-patterns", "pre-vapt-secure-coding"],
                },
                {
                    "name": "qa-engineer",
                    "description": "Risk-based tests. Use after features or before release.",
                    "system_prompt": qa_prompt,
                    "tools": ["Read", "Grep", "Glob", "Shell"],
                    "skills": ["test-strategy", "pre-vapt-secure-coding"],
                },
                {
                    "name": "devops-engineer",
                    "description": "CI/CD and secret hygiene. Use for pipelines and deploy config.",
                    "system_prompt": devops_prompt,
                    "tools": ["Read", "Write", "Shell"],
                    "skills": ["ci-cd-hardening"],
                },
                {
                    "name": "security-vapt-reviewer",
                    "description": (
                        "Pre-VAPT AppSec gate: SQL/XSS/log/command injection, IDOR, SSRF, "
                        "and other common findings. Use before pen-test handoff."
                    ),
                    "system_prompt": security_prompt,
                    "tools": ["Read", "Grep", "Glob"],
                    "skills": ["pre-vapt-secure-coding"],
                },
            ],
            "skills": [
                {
                    "name": "api-design",
                    "description": "Designs versioned APIs with schema validation. Use when changing endpoints or webhooks.",
                    "instructions": _clip_lines(
                        f"""# API Design
Context: {hint}

## Steps
1. Name resource + version path.
2. Validate bodies with schema libs (not regex).
3. Document authZ and idempotency keys.

## Example
Input: "POST refund for payment_id"
Output: `POST /api/v1/payments/{{id}}/refunds` with Pydantic model, 409 on duplicate Idempotency-Key.
"""
                    ),
                },
                {
                    "name": "ui-patterns",
                    "description": "Accessible UI patterns. Use when building screens or forms.",
                    "instructions": _clip_lines(
                        f"""# UI Patterns
Context: {hint}

1. One primary CTA per view.
2. Typed reactive forms + server errors.
3. Encode user HTML; never build RegExp from search boxes.

## Example
Search merchants → debounce + API filter params, not `new RegExp(userQuery)`.
"""
                    ),
                },
                {
                    "name": "test-strategy",
                    "description": "Layered tests including pre-VAPT cases. Use for features and releases.",
                    "instructions": _clip_lines(
                        f"""# Test Strategy
Context: {hint}

- Unit: domain money rules
- Integration: auth boundaries
- Negatives: injection, IDOR, overlong strings

## Example
Tenant A JWT must 403 on Tenant B ledger account id.
"""
                    ),
                },
                {
                    "name": "ci-cd-hardening",
                    "description": "Hardens CI/CD. Use for build, deploy, or secret config changes.",
                    "instructions": _clip_lines(
                        """# CI/CD
1. Lint + unit tests
2. CVE scan + SAST
3. Fail on plaintext secrets in env files

## Example
Task definition with `PASSWORD=...` → reject; use secret ARN.
"""
                    ),
                },
                {
                    "name": "pre-vapt-secure-coding",
                    "description": (
                        "Common VAPT checklist: SQLi, XSS, log injection, command injection, "
                        "IDOR, SSRF, CSRF, path traversal, ReDoS, secrets hygiene."
                    ),
                    "instructions": _clip_lines(
                        f"""# Pre-VAPT Secure Coding
Context: {hint}

## Checklist (must pass)
- SQL/NoSQL: parameterized / ORM — never concat
- XSS: encode/escape all untrusted output; CSP where UI
- Log injection: sanitize newlines/control chars; no raw user text in log lines
- No shell/SQL/HTML/path built from user input
- AuthZ/IDOR on every object; CSRF if cookie sessions
- Block SSRF (allowlist hosts); no XXE; no mass assignment
- No user-controlled regex (ReDoS); no secrets/PII in logs

## Example
User search logged as f"q={{q}}" with newlines
→ Reject; escape/strip CR/LF or structured logging fields only.

## Script
Run `scripts/validate_checks.py` for a deterministic checklist.
"""
                    ),
                    "references": {
                        "scripts/validate_checks.py": (
                            "#!/usr/bin/env python3\n"
                            "CHECKS = [\n"
                            '    "sql_injection_parameterized",\n'
                            '    "xss_output_encoding",\n'
                            '    "log_injection_sanitized",\n'
                            '    "command_injection_no_shell",\n'
                            '    "idor_object_authz",\n'
                            '    "ssrf_allowlist",\n'
                            '    "csrf_or_bearer",\n'
                            '    "path_traversal_canonical",\n'
                            '    "redos_no_user_regex",\n'
                            '    "no_secrets_in_logs",\n'
                            "]\n"
                            'print("pre-vapt checklist:")\n'
                            "for c in CHECKS:\n"
                            '    print(f"  [ ] {c}")\n'
                            'print("pre-vapt checklist ok")\n'
                        )
                    },
                },
            ],
            "rules": [
                {
                    "name": "core-standards",
                    "description": "Project-wide delivery standards",
                    "body": _clip_lines(
                        f"""# Core
Context: {hint}
- Small focused PRs
- No secrets in repo
- Validate at trust boundaries
"""
                    ),
                    "always_apply": True,
                    "globs": [],
                },
                {
                    "name": "secure-coding-vapt",
                    "description": (
                        "Always-on VAPT gate: no SQLi, XSS, log injection, or other common "
                        "injection/authZ flaws"
                    ),
                    "body": _clip_lines(
                        """# Secure Coding (VAPT Gate)
Never ship:
- SQL/NoSQL built from string concat
- Unencoded user HTML/JS (XSS)
- Raw untrusted input in log lines (log injection)
- User input in shell, paths, regex, or outbound URLs (cmd/path/ReDoS/SSRF)
- Missing object-level authZ (IDOR) or open mass assignment
Fix or waive High/Critical before VAPT handoff.
"""
                    ),
                    "always_apply": True,
                    "globs": [],
                },
                {
                    "name": "backend-files",
                    "description": "Backend conventions for API and services",
                    "body": _clip_lines(
                        """# Backend
- Thin controllers / routers
- Explicit DTOs
- Indexed FKs; tenant_id on multi-tenant tables
"""
                    ),
                    "always_apply": False,
                    "globs": [
                        "backend/api/routes/**/*.py",
                        "backend/services/**/*.py",
                        "backend/repositories/**/*.py",
                    ],
                },
            ],
            "mcp_servers": [
                {
                    "name": "filesystem",
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-filesystem", "${workspaceFolder}"],
                    "env": {},
                }
            ]
            if brief.integrations
            else [],
            "source_tree": [
                f.model_dump()
                for f in build_source_tree_from_brief(
                    brief, project_name=name, summary=summary
                )
            ],
        }
    plan = ProjectPlan.model_validate(payload)
    sdd = fallback_solution_design(brief, plan, platform)
    return plan.model_copy(
        update={
            "work_breakdown": fallback_work_breakdown(brief, plan, platform),
            "work_breakdown_llm": False,
            # The deterministic SDD is structurally complete by construction, so demo mode
            # marks it so — otherwise every demo export would try to regenerate it.
            "solution_design": sdd,
            "solution_design_llm": False,
            "solution_design_complete": is_solution_design_complete(sdd),
            "architecture_views": fallback_solution_design_views(brief, plan),
        }
    )
