"""Tests for agent ## Goals bullet normalization."""

from app.models.schemas import AgentSpec, ProjectPlan
from app.services.deduction.goals_format import ensure_goals_bullets, normalize_plan_agent_goals

VAPT_PARAGRAPH = """# Role
You are a security-focused VAPT reviewer for Smart Tutor AI.

## Goals
Block High/Critical vulnerabilities before merge. Enforce parameterized queries (SQLAlchemy ORM only; no raw SQL), output encoding (Jinja2 autoescaping, explicit HTML sanitization), log injection prevention (sanitize CR/LF in all untrusted input before logging), command injection prevention (no shell=True; validate file paths), path traversal prevention (resolve and validate all file operations against allowed directories), SSRF prevention (validate and allowlist URLs for external calls to Bedrock/Pinecone/S3/Polly/Transcribe), IDOR/broken authZ prevention (verify process/batch/enrollment ownership in every data access; enforce trainer approval status checks), CSRF protection (SameSite cookies, CSRF tokens for state-changing operations), mass assignment prevention (explicit Pydantic field allowlists), XXE/insecure deserialization prevention (safe XML parsers; no pickle/eval on untrusted data), ReDoS prevention (reject user-supplied regex; timeout AI-generated patterns), secrets/PII protection (never log JWT tokens, passwords, bcrypt hashes, SSO tokens, API keys, email addresses, or assessment answers).

## Workflow
1. Read modified files.
2. Demand fixes for High/Critical.
"""


def test_vapt_goals_become_bullets():
    out = ensure_goals_bullets(VAPT_PARAGRAPH)
    goals_block = out.split("## Goals", 1)[1].split("## Workflow", 1)[0]
    lines = [ln for ln in goals_block.splitlines() if ln.strip()]
    assert lines, "Goals section should not be empty"
    assert all(ln.startswith("- ") for ln in lines)
    assert any("SQL" in ln or "parameterized" in ln for ln in lines)
    assert any("XSS" in ln or "encoding" in ln.lower() for ln in lines)
    assert any("log injection" in ln.lower() for ln in lines)
    assert "Block High/Critical" in goals_block
    # Workflow preserved
    assert "## Workflow" in out
    assert "Demand fixes" in out


def test_already_bullets_unchanged_shape():
    md = """# Role
Tutor.

## Goals
- Plan lessons
- Track progress

## Workflow
1. Do work
"""
    out = ensure_goals_bullets(md)
    assert "- Plan lessons" in out
    assert "- Track progress" in out


def test_numbered_goals_become_dashes():
    md = """# Role
Sec.

## Goals
1. Detect IDOR on every lookup
2. Flag PHI leaks in logs
3. Catch unsafe RegExp

## Workflow
1. Review
"""
    out = ensure_goals_bullets(md)
    block = out.split("## Goals", 1)[1].split("## Workflow", 1)[0]
    lines = [ln for ln in block.splitlines() if ln.strip()]
    assert all(ln.startswith("- ") for ln in lines)
    assert "Detect IDOR" in block


def test_vapt_core_responsibilities_renamed():
    md = """# Security VAPT Reviewer
You gate FleetPulse.

## Core Responsibilities
1. **Tenant Isolation (IDOR)** – Verify every API endpoint enforces org_id
2. **Authorization** – Confirm RBAC on all routes
3. **ReDoS Prevention** – Reject hand-rolled regex

## Example
Bad regex → reject.
"""
    out = ensure_goals_bullets(md, prefer_vapt_alt=True)
    assert "## Goals" in out
    assert "## Core Responsibilities" not in out
    block = out.split("## Goals", 1)[1].split("## Example", 1)[0]
    lines = [ln for ln in block.splitlines() if ln.strip()]
    assert all(ln.startswith("- ") for ln in lines)
    assert any("Tenant Isolation" in ln for ln in lines)


def test_normalize_plan():
    plan = ProjectPlan(
        project_name="Smart Tutor",
        summary="x",
        agents=[
            AgentSpec(
                name="security-vapt-reviewer",
                description="VAPT",
                system_prompt=VAPT_PARAGRAPH,
                tools=["Read"],
                skills=[],
            )
        ],
    )
    fixed = normalize_plan_agent_goals(plan)
    assert fixed is not plan
    goals = fixed.agents[0].system_prompt.split("## Goals", 1)[1].split("## ", 1)[0]
    assert all(ln.startswith("- ") for ln in goals.splitlines() if ln.strip())
