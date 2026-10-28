"""
Add users.must_change_password, the first-login password change flag.

Set on every account an admin creates (single or bulk upload), which starts on the
St@<EmployeeID>#2026 login. Existing rows get FALSE, so no current user is affected.

The column is added only if absent: on a new database Base.metadata.create_all runs before
Alembic and has already created it from the model, so an unconditional ADD COLUMN would fail
there.

Revision ID: 0002_must_change_password
Revises: 0001_baseline
Create Date: 2026-10-08
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_must_change_password"
down_revision: Union[str, Sequence[str], None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "users"
_COLUMN = "must_change_password"


def _has_column() -> bool:
    return _COLUMN in {column["name"] for column in sa.inspect(op.get_bind()).get_columns(_TABLE)}


def upgrade() -> None:
    if not _has_column():
        op.add_column(
            _TABLE,
            sa.Column(_COLUMN, sa.Boolean(), nullable=False, server_default=sa.false()),
        )


def downgrade() -> None:
    # Written by hand on purpose: this revision introduced the column, so removing it is the
    # exact reverse. It holds only a flag, never user content.
    if _has_column():
        op.drop_column(_TABLE, _COLUMN)
