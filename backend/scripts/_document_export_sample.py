"""
Render a real SDD.md to PDF and DOCX in `.scratch/`, for the eyeball pass.

    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/_document_export_sample.py

No Bedrock and no database: the document comes from `fallback_solution_design` and the three
figures from `render_architecture`, which is the same content the export path produces when the
model is unavailable. Prints a block census so a silently-dropped table or figure shows up as a
number rather than as something to notice by scrolling.
"""

from __future__ import annotations

import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.state_machine import Platform  # noqa: E402
from app.services.deduction.solution_design import (  # noqa: E402
    fallback_solution_design,
    fallback_solution_design_views,
)
from app.services.diagrams.architecture import (  # noqa: E402
    ARCH_WORKSPACE_PATHS,
    render_architecture,
)
from app.services.export import documents  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_solution_design import sample_brief, sample_plan  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / ".scratch"


def main() -> int:
    brief, plan = sample_brief(), sample_plan()
    md = fallback_solution_design(brief, plan, Platform.CLAUDE_CODE)
    views = fallback_solution_design_views(brief, plan)

    drawn = render_architecture(views, project_name=plan.project_name)
    images = {
        path: drawn[kind][0] for path, kind in ARCH_WORKSPACE_PATHS.items() if kind in drawn
    }
    print(f"markdown: {len(md)} chars, figures rendered: {len(images)}/3")

    blocks = documents.parse_markdown(md)
    census = collections.Counter(b.kind for b in blocks)
    print("blocks:  " + ", ".join(f"{k}={v}" for k, v in sorted(census.items())))
    print(f"referenced figures: {documents.image_paths(md)}")
    missing = [p for p in documents.image_paths(md) if p not in images]
    print(f"unresolved figures: {missing or 'none'}")

    # Every table's column count, so a ragged row that lost a column is visible here.
    for i, block in enumerate(b for b in blocks if b.kind == "table"):
        widths = {len(r) for r in block.rows}
        print(
            f"  table {i + 1}: {len(block.headers)} cols, {len(block.rows)} rows, "
            f"row widths {sorted(widths) or '-'}, aligns {list(block.aligns)}"
        )

    OUT.mkdir(parents=True, exist_ok=True)
    for fmt in documents.DOC_FORMATS:
        data = documents.render_document(
            md,
            fmt,
            title=plan.project_name,
            subtitle="Solution Design Document (SDD.md)",
            images=images,
        )
        target = OUT / f"SDD-sample.{fmt}"
        target.write_bytes(data)
        print(f"wrote {target} — {len(data) / 1024:.1f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
