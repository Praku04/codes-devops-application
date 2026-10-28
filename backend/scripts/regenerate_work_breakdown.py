"""Regenerate WORKBREAKDOWN.md for an existing project (CLI — uses Bedrock from .env)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_root = Path(__file__).resolve().parents[2]
_env = _root / ".env"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        import os

        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.session import init_db, repo  # noqa: E402
from app.services.deduction.work_breakdown import (  # noqa: E402
    apply_work_breakdown_to_plan,
    is_work_breakdown_complete,
)
from app.services.projects import (  # noqa: E402
    _plan_with_source_tree,
    generate_detailed_work_breakdown,
)


async def run(project_id: str) -> None:
    init_db()
    status = repo.get(project_id)
    if not status or not status.plan:
        print(f"Project not found or no plan: {project_id}")
        sys.exit(1)
    print(f"Project: {status.name}")
    print(f"Platform: {status.platform}")
    plan = _plan_with_source_tree(status)

    async def on_progress(msg: str) -> None:
        print(msg, flush=True)

    async def on_partial(partial_md: str) -> None:
        cur = repo.get(project_id)
        pl = _plan_with_source_tree(cur)
        updated = apply_work_breakdown_to_plan(
            pl, partial_md, used_llm=False, complete=False
        )
        repo.update(project_id, plan=updated)
        print(f"  … saved partial ({len(partial_md)} chars)", flush=True)

    print("Starting Bedrock WORKBREAKDOWN (concise, single pass; ~2-4 min)…", flush=True)
    result = await generate_detailed_work_breakdown(
        status,
        plan,
        on_progress=on_progress,
        on_partial=on_partial,
    )
    body = result.work_breakdown or ""
    ok = is_work_breakdown_complete(body)
    print(f"\nDone. chars={len(body)} complete={ok} llm={result.work_breakdown_llm}")
    if not ok:
        sys.exit(2)


def main() -> None:
    pid = sys.argv[1] if len(sys.argv) > 1 else "2e3d797b-1ad8-45f0-8899-57f31fa31235"
    asyncio.run(run(pid))


if __name__ == "__main__":
    main()
