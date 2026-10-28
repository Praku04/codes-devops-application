"""Uploaded documents are persisted in SQLite and listable by the UI and CLI."""

from unittest.mock import AsyncMock, patch

import pytest

from app.core.state_machine import ProjectPath, ProjectState
from app.db.session import backfill_brief_documents
from app.models.schemas import ProjectBrief, UploadedDocument
from app.services.parser.documents import guess_content_type
from app.services.projects import project_service, repo

STATEMENT = "Build Smart Tutor AI that turns company SOPs into training modules."


def _docs_project(user_id: str = "doc-tester"):
    status = project_service.create(user_id=user_id)
    return project_service.set_path(status.id, ProjectPath.DOCS)


async def _submit(project_id: str, files: list[tuple[str, bytes]]):
    # The semantic gate calls Bedrock; that behaviour is covered by test_docs_match.
    with patch(
        "app.services.parser.match.assert_statement_matches_documents",
        new=AsyncMock(return_value=None),
    ):
        return await project_service.submit_documents(
            project_id, problem_statement=STATEMENT, files=files
        )


@pytest.mark.asyncio
async def test_upload_records_each_document():
    status = _docs_project()
    body_a = b"# Smart Tutor BRD\nSOP ingestion, module generation, quizzes.\n"
    body_b = b"Role-based learning paths for the workforce.\n"

    result = await _submit(status.id, [("brd.md", body_a), ("notes.txt", body_b)])

    assert result.state == ProjectState.CONTEXT_READY
    docs = result.brief.documents
    assert [d.filename for d in docs] == ["brd.md", "notes.txt"]
    assert [d.size_bytes for d in docs] == [len(body_a), len(body_b)]
    assert docs[0].content_type == "text/markdown"
    assert docs[1].content_type == "text/plain"
    assert all(d.chars_extracted > 0 for d in docs)
    # Timestamps make the upload attributable; format is ISO-8601 UTC.
    assert all(d.uploaded_at and d.uploaded_at.endswith("+00:00") for d in docs)


@pytest.mark.asyncio
async def test_document_records_survive_reload_from_sqlite():
    """Records live in the brief_json column — no new column, no migration."""
    status = _docs_project()
    await _submit(status.id, [("spec.md", b"Restocking rules for the warehouse.\n")])

    fresh = repo.get(status.id)
    assert fresh is not None
    assert [d.filename for d in fresh.brief.documents] == ["spec.md"]


@pytest.mark.asyncio
async def test_session_summary_exposes_document_names():
    """The UI's Documents chips and the CLI's Documents column read this field."""
    status = _docs_project(user_id="doc-summary-tester")
    await _submit(status.id, [("brd.md", b"SOP training modules and quizzes.\n")])

    rows = repo.list_sessions("doc-summary-tester")
    row = next(r for r in rows if r.id == status.id)
    assert row.document_names == ["brd.md"]


@pytest.mark.asyncio
async def test_reupload_replaces_the_document_list():
    """Re-attaching files is a replace, not an append — the UI shows one set."""
    status = _docs_project()
    await _submit(status.id, [("old.md", b"First upload about SOP training.\n")])
    project_service.rollback(status.id)
    result = await _submit(status.id, [("new.md", b"Second upload about SOP training.\n")])

    assert [d.filename for d in result.brief.documents] == ["new.md"]


def test_backfill_reconstructs_documents_for_existing_projects():
    """Pre-upgrade rows only have concatenated text; the headers are enough."""
    status = project_service.create(user_id="doc-backfill-tester")
    project_service.set_path(status.id, ProjectPath.DOCS)
    repo.update(
        status.id,
        state=ProjectState.CONTEXT_READY,
        brief=ProjectBrief(
            problem_statement=STATEMENT,
            document_text="### brd.md\nSOP modules.\n\n### notes.txt\nQuiz rules.\n",
        ),
    )

    assert backfill_brief_documents() >= 1

    fixed = repo.get(status.id)
    assert [d.filename for d in fixed.brief.documents] == ["brd.md", "notes.txt"]
    # Original byte sizes were never recorded, so the UI/CLI hide the size.
    assert all(d.size_bytes == 0 for d in fixed.brief.documents)
    assert all(d.chars_extracted > 0 for d in fixed.brief.documents)
    assert fixed.brief.documents[0].content_type == guess_content_type("brd.md")


def test_backfill_ignores_markdown_headings_inside_a_document():
    """A document's own "### 2.1 Problem Statement" is body text, not an upload."""
    status = project_service.create(user_id="doc-heading-tester")
    project_service.set_path(status.id, ProjectPath.DOCS)
    repo.update(
        status.id,
        state=ProjectState.CONTEXT_READY,
        brief=ProjectBrief(
            problem_statement=STATEMENT,
            document_text=(
                "### BRD.md\n"
                "# Smart Tutor BRD\n"
                "### 2.1 Problem Statement\n"
                "SOP training is slow.\n"
                "### 2.2 Business Opportunity\n"
                "Scale onboarding.\n\n"
                "### guide.docx\n"
                "### 4.3 Trainer Responsibilities\n"
                "Publish modules.\n"
            ),
        ),
    )

    backfill_brief_documents()

    docs = repo.get(status.id).brief.documents
    assert [d.filename for d in docs] == ["BRD.md", "guide.docx"]
    # Heading text stays with its file, so the character count includes it.
    assert "2.1 Problem Statement" not in " ".join(d.filename for d in docs)
    assert docs[0].chars_extracted > len("# Smart Tutor BRD")


def test_backfill_repairs_rows_written_by_the_buggy_first_pass():
    """Rows that already stored heading text as filenames must be corrected."""
    status = project_service.create(user_id="doc-repair-tester")
    project_service.set_path(status.id, ProjectPath.DOCS)
    repo.update(
        status.id,
        state=ProjectState.CONTEXT_READY,
        brief=ProjectBrief(
            problem_statement=STATEMENT,
            document_text="### BRD.md\n### 2.1 Problem Statement\nSOP training is slow.\n",
            documents=[
                UploadedDocument(filename="BRD.md"),
                UploadedDocument(filename="2.1 Problem Statement"),
            ],
        ),
    )

    assert backfill_brief_documents() >= 1
    assert [d.filename for d in repo.get(status.id).brief.documents] == ["BRD.md"]


def test_backfill_is_idempotent():
    status = project_service.create(user_id="doc-idem-tester")
    project_service.set_path(status.id, ProjectPath.DOCS)
    repo.update(
        status.id,
        state=ProjectState.CONTEXT_READY,
        brief=ProjectBrief(
            problem_statement=STATEMENT,
            document_text="### only.md\nOne section.\n",
        ),
    )

    backfill_brief_documents()
    before = repo.get(status.id).brief.documents
    # A second pass must not duplicate or rewrite anything.
    assert backfill_brief_documents() == 0
    assert repo.get(status.id).brief.documents == before


def test_backfill_skips_interview_projects_without_documents():
    status = project_service.create(user_id="doc-skip-tester")
    project_service.set_path(status.id, ProjectPath.INTERVIEW)

    backfill_brief_documents()

    reloaded = repo.get(status.id)
    assert reloaded.brief is None or reloaded.brief.documents == []


def test_uploaded_document_defaults_are_safe_for_old_payloads():
    """Rows written before this field existed must still validate."""
    brief = ProjectBrief.model_validate({"problem_statement": "x", "document_text": "y"})
    assert brief.documents == []
    doc = UploadedDocument(filename="a.md")
    assert (doc.size_bytes, doc.chars_extracted, doc.uploaded_at) == (0, 0, None)
