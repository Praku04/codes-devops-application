from app.core.state_machine import Platform
from app.models.schemas import ProjectBrief, SourceFileSpec
from app.services.deduction.demo_fallback import build_demo_plan
from app.services.deduction.source_tree import (
    docstring_only_content,
    ensure_package_inits,
    ensure_root_scaffold_files,
    sanitize_source_tree_docs_only,
    source_tree_needs_doc_sanitize,
    source_tree_needs_root_scaffold,
)
from app.services.export.renderers import render_files


def test_sanitize_strips_executable_scaffold():
    dirty = [
        SourceFileSpec(
            path="backend/services/auth_service.py",
            purpose="Auth service",
            content=(
                '"""backend/services/auth_service.py\n\nRole: Auth\n"""\n'
                "from __future__ import annotations\n\n"
                "class AuthService:\n"
                "    def create(self):\n"
                "        raise NotImplementedError\n"
            ),
        )
    ]
    assert source_tree_needs_doc_sanitize(dirty)
    clean = sanitize_source_tree_docs_only(dirty)
    auth = next(f for f in clean if f.path.endswith("auth_service.py"))
    assert "class " not in auth.content
    assert "import " not in auth.content
    assert "Role:" in auth.content


def test_export_forces_docstring_only_even_if_plan_polluted():
    plan = build_demo_plan(ProjectBrief(problem_statement="payments API"), Platform.CURSOR)
    polluted = SourceFileSpec(
        path="backend/services/auth_service.py",
        purpose="Business rules for auth.",
        content=(
            '"""x"""\nfrom backend.repositories.auth_repository import AuthRepository\n'
            "class AuthService:\n    pass\n"
        ),
    )
    plan = plan.model_copy(update={"source_tree": [polluted, *plan.source_tree]})
    files = render_files(plan, Platform.CURSOR)
    body = files["backend/services/auth_service.py"]
    assert "class " not in body
    assert "from backend" not in body
    assert "import " not in body
    assert "Role:" in body
    assert body.strip().startswith('"""')
    assert body.strip().endswith('"""')


def test_root_scaffold_files_added_when_missing():
    bare = [
        SourceFileSpec(path="main.py", purpose="Entry", content='"""main"""\n'),
        SourceFileSpec(path="backend/__init__.py", purpose="Pkg", content='"""pkg"""\n'),
    ]
    assert source_tree_needs_root_scaffold(bare)
    enriched = ensure_package_inits(bare)
    paths = {f.path for f in enriched}
    assert "requirements.txt" in paths
    assert ".env.example" in paths
    assert ".env" in paths
    assert not source_tree_needs_root_scaffold(enriched)


def test_export_includes_requirements_and_env():
    plan = build_demo_plan(ProjectBrief(problem_statement="payments API"), Platform.CURSOR)
    files = render_files(plan, Platform.CURSOR)
    assert "requirements.txt" in files
    assert ".env.example" in files
    assert ".env" in files
    assert "Role:" in files["requirements.txt"]
