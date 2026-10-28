"""
The two file counts the UI, README, and CLI all show must agree.

A "Files (139)" badge next to a "165 workspace files" message looked like a desync when
it was really two different quantities with one name between them. These tests pin the
relationship: source files are the app scaffold, workspace files are everything exported,
and the README states the same numbers the tree does.
"""

import re

import pytest

from app.core.state_machine import Platform
from app.models.schemas import ProjectBrief, SourceFileSpec
from app.services.deduction.demo_fallback import build_demo_plan
from app.services.export.renderers import (
    WORKSPACE_COUNT_TOKEN,
    _is_ide_or_readme,
    render_files,
    scaffold_file_count,
)
from app.services.deduction.work_breakdown import WORK_BREAKDOWN_FILENAME

PLATFORMS = [Platform.CURSOR, Platform.CLAUDE_CODE, Platform.WINDSURF, Platform.GITHUB_COPILOT,]


def _plan(platform: Platform):
    return build_demo_plan(ProjectBrief(problem_statement="payments API with ledger"), platform)


def _readme(files: dict[str, str]) -> str:
    return files["README.md"]


@pytest.mark.parametrize("platform", PLATFORMS)
def test_readme_reports_the_real_workspace_total(platform: Platform):
    """The count is substituted after rendering, so it cannot drift from the file set."""
    plan = _plan(platform)
    files = render_files(plan, platform)

    stated = int(re.search(r"Workspace files: (\d+)", _readme(files)).group(1))
    assert stated == len(files)


@pytest.mark.parametrize("platform", PLATFORMS)
def test_no_unsubstituted_placeholder_survives(platform: Platform):
    """A leaked token would show up verbatim in the exported README."""
    files = render_files(_plan(platform), platform)
    assert not [p for p, body in files.items() if WORKSPACE_COUNT_TOKEN in body]


@pytest.mark.parametrize("platform", PLATFORMS)
def test_readme_source_count_matches_the_scaffold_helper(platform: Platform):
    """This is the number the UI's Files tab shows, so both must come out the same."""
    plan = _plan(platform)
    files = render_files(plan, platform)

    stated = int(re.search(r"Source files: (\d+)", _readme(files)).group(1))
    assert stated == scaffold_file_count(plan)


@pytest.mark.parametrize("platform", PLATFORMS)
def test_workspace_is_larger_than_source_by_the_ide_artifacts(platform: Platform):
    """
    The gap the screenshots showed: workspace = scaffold + IDE folder + README + WBS.

    Asserted as an exact partition of the rendered paths, so it stays true as artifact
    counts change rather than pinning a magic number.
    """
    plan = _plan(platform)
    files = render_files(plan, platform)

    ide = [p for p in files if _is_ide_or_readme(p)]
    scaffold = [p for p in files if not _is_ide_or_readme(p) and p != WORK_BREAKDOWN_FILENAME]

    # Every path is exactly one of: IDE artifact, app scaffold, or the work breakdown.
    assert len(files) == len(ide) + len(scaffold) + 1
    assert len(scaffold) == scaffold_file_count(plan)
    assert len(files) > scaffold_file_count(plan), "workspace must include IDE artifacts"


@pytest.mark.parametrize("platform", PLATFORMS)
def test_scaffold_count_ignores_ide_paths_in_source_tree(platform: Platform):
    """
    An IDE path that leaks into source_tree must not inflate the Files badge.

    The exporter skips such entries when writing, so counting the raw list length would
    promise a file the export never contains.
    """
    plan = _plan(platform)
    before = scaffold_file_count(plan)
    polluted = plan.model_copy(
        update={
            "source_tree": [
                *(plan.source_tree or []),
                SourceFileSpec(path=".cursor/agents/stray.md", purpose="leaked IDE artifact"),
                SourceFileSpec(path="README.md", purpose="leaked readme"),
            ]
        }
    )
    assert scaffold_file_count(polluted) == before


def test_preview_and_zip_see_the_same_paths():
    """The tree lists preview keys; the zip is built from the same dict."""
    plan = _plan(Platform.CURSOR)
    files = render_files(plan, Platform.CURSOR)
    # Rendering twice is stable — the count in the README stays correct on refresh.
    again = render_files(plan, Platform.CURSOR)
    assert sorted(files) == sorted(again)
    assert _readme(files) == _readme(again)
