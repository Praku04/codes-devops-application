"""Frontend scaffold backfill for existing plans."""

from app.models.schemas import ProjectBrief, SourceFileSpec
from app.services.deduction.source_tree import (
    ensure_frontend_in_tree,
    source_tree_needs_frontend,
)


def test_learning_brief_backfills_frontend():
    brief = ProjectBrief(
        problem_statement="Enterprise AI learning platform with trainee dashboard and trainer portal.",
        tech_stack="FastAPI, Jinja2 templates, browser SPA",
    )
    backend_only = [
        SourceFileSpec(path="main.py", purpose="entry"),
        SourceFileSpec(path="backend/api/routes/courses.py", purpose="courses"),
    ]
    assert source_tree_needs_frontend(backend_only, brief, "Smart Tutor")
    merged = ensure_frontend_in_tree(
        backend_only,
        brief,
        project_name="smart-tutor",
        summary="AI tutoring platform",
    )
    paths = {f.path for f in merged}
    assert any(p.startswith("frontend/") for p in paths)
    assert "frontend/templates/base.html" in paths or "frontend/app/page.tsx" in paths


def test_backend_only_api_skips_frontend():
    brief = ProjectBrief(problem_statement="Headless REST API for webhooks only.")
    files = [SourceFileSpec(path="main.py", purpose="entry")]
    assert not source_tree_needs_frontend(files, brief, "")
