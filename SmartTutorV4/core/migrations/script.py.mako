"""
${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

Before committing, read every operation below:
- No drop_table / drop_column / drop_index / drop_constraint unless it was written by hand on
  purpose (autogenerate's are stripped - see core.schema_migrations).
- alter_column with nullable=False fails the deploy if any existing row is NULL: backfill first.
- alter_column with type_= rewrites the table; check timestamp time zone changes in particular.
- downgrade() must undo upgrade() exactly.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision: str = ${repr(up_revision)}
down_revision: Union[str, Sequence[str], None] = ${repr(down_revision)}
branch_labels: Union[str, Sequence[str], None] = ${repr(branch_labels)}
depends_on: Union[str, Sequence[str], None] = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
