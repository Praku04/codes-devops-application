"""
Baseline: the schema as the application built it before versioned migrations existed.

Everything up to this point is created idempotently at startup by Base.metadata.create_all,
ensure_assessment_tables_exist() and add_missing_columns() in core/runtime.py, which run before
Alembic does. This revision therefore changes nothing: applying it to an existing database only
records the version. It is the point from which every further schema change is a numbered
revision with a downgrade.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-05
"""

from typing import Sequence, Union

revision: str = "0001_baseline"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Nothing to apply: the startup steps in core/runtime.py already built this schema."""


def downgrade() -> None:
    """Nothing to undo: the baseline introduced no schema of its own."""
