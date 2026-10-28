"""
Versioned schema migrations (Alembic) for Smart Tutor.

At startup, core.runtime.create_db_and_tables() calls upgrade_to_head() while it holds the schema
advisory lock, after the legacy idempotent steps. From the repository root:

    python -m core.schema_migrations current
    python -m core.schema_migrations history
    python -m core.schema_migrations upgrade [revision]      (default: head)
    python -m core.schema_migrations downgrade <revision>

upgrade and downgrade take the same advisory lock as startup, so they cannot interleave with a
container that is starting. upgrade runs the legacy steps first, exactly as startup does, so it is
a complete replacement for startup migrations when run as a deploy step. To draft a new revision, run ``alembic revision --autogenerate -m "..."``
from the repository root, then read the generated file before committing it.
"""

import argparse
import logging
import sys
import time
from functools import lru_cache
from pathlib import Path
from typing import List, Optional, Set, Tuple

from alembic import command
from alembic.config import Config
from alembic.operations import ops
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

# Safety net for an operator command, longer than startup's: someone ran it on purpose.
_CLI_LOCK_WAIT_SECONDS = 300

_OpKey = Tuple[str, str]


def alembic_config() -> Config:
    """Alembic configuration built in code, so no database URL or credential lives in a file."""
    cfg = Config()
    # configparser interpolation treats "%" specially; a path containing one must be escaped.
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR).replace("%", "%%"))
    return cfg


# A migration's DDL (ALTER TABLE ...) needs an exclusive lock on its table. Behind live traffic an
# open transaction can hold that table, and with no limit the ALTER waits for it indefinitely: the new
# container never finishes starting (so ECS rolls it back while the pipeline still reports success),
# and every other query on that table - logins on `users` - queues behind the waiting ALTER. So each
# attempt waits only briefly for the lock, and is retried after a pause that lets that traffic run.
_MIGRATION_RETRY_PAUSE_SECONDS = 2.0
# Safety net: below core.runtime's 180 s schema-lock wait, so the other workers are still waiting
# when this gives up.
_MIGRATION_RETRY_BUDGET_SECONDS = 150.0
_LOCK_NOT_AVAILABLE = "55P03"


@lru_cache(maxsize=1)
def head_revision() -> str:
    """The newest revision in core/migrations/versions."""
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def current_revision(engine) -> Optional[str]:
    """The revision the database is at, or None if it has never been migrated."""
    try:
        with engine.connect() as connection:
            return connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except ProgrammingError:
        # No alembic_version table yet: a database that predates versioned migrations.
        return None


def schema_is_current(engine) -> bool:
    """True if every revision is applied. Connection errors propagate to the caller."""
    return current_revision(engine) == head_revision()


def _is_lock_timeout(exc: Exception) -> bool:
    return getattr(getattr(exc, "orig", None), "pgcode", None) == _LOCK_NOT_AVAILABLE


def upgrade_to_head(engine) -> None:
    """Apply every pending revision; see _run_revisions."""
    _run_revisions(engine, "upgrade", "head")


def _run_revisions(engine, action: str, revision: str) -> None:
    """
    Upgrade or downgrade to a revision in one transaction - Postgres DDL is transactional.

    A lock timeout rolls the attempt back and it is retried; any other error, or running out of
    retries, is raised to the caller.
    """
    run = command.upgrade if action == "upgrade" else command.downgrade
    cfg = alembic_config()
    deadline = time.monotonic() + _MIGRATION_RETRY_BUDGET_SECONDS
    attempt = 0
    while True:
        attempt += 1
        try:
            with engine.begin() as connection:
                # SET LOCAL: scoped to this transaction, so it never reaches a pooled connection.
                connection.execute(text("SET LOCAL lock_timeout = '5s'"))
                cfg.attributes["connection"] = connection
                run(cfg, revision)
            return
        except OperationalError as exc:
            if not _is_lock_timeout(exc) or time.monotonic() >= deadline:
                raise
            logger.warning(
                "Schema migration is waiting for a table that live traffic holds (attempt %s); retrying.",
                attempt,
            )
            time.sleep(_MIGRATION_RETRY_PAUSE_SECONDS)


def _drop_key(op) -> Optional[_OpKey]:
    if isinstance(op, ops.DropTableOp):
        return ("table", op.table_name)
    if isinstance(op, ops.DropColumnOp):
        return ("column", f"{op.table_name}.{op.column_name}")
    if isinstance(op, ops.DropIndexOp):
        return ("index", op.index_name)
    if isinstance(op, ops.DropConstraintOp):
        return ("constraint", op.constraint_name)
    return None


def _restore_key(op) -> Optional[_OpKey]:
    if isinstance(op, ops.CreateTableOp):
        return ("table", op.table_name)
    if isinstance(op, ops.AddColumnOp):
        return ("column", f"{op.table_name}.{op.column.name}")
    if isinstance(op, ops.CreateIndexOp):
        return ("index", op.index_name)
    if isinstance(op, ops.AddConstraintOp):
        return ("constraint", op.constraint_name)
    return None


def _without_drops(op_list: List, removed: Set[_OpKey]) -> List:
    kept = []
    for op in op_list:
        if isinstance(op, ops.OpContainer):
            op.ops = _without_drops(op.ops, removed)
            if op.ops:
                kept.append(op)
            continue
        key = _drop_key(op)
        if key is not None:
            removed.add(key)
            continue
        kept.append(op)
    return kept


def _without_restores(op_list: List, removed: Set[_OpKey]) -> List:
    kept = []
    for op in op_list:
        if isinstance(op, ops.OpContainer):
            op.ops = _without_restores(op.ops, removed)
            if op.ops:
                kept.append(op)
            continue
        if _restore_key(op) in removed:
            continue
        kept.append(op)
    return kept


def drop_destructive_autogenerate_ops(_context, _revision, directives) -> None:
    """
    Keep autogenerate from writing DROP TABLE, DROP COLUMN, DROP INDEX or DROP CONSTRAINT.

    The databases were built partly by Base.metadata.create_all and partly by hand-written CREATE
    TABLE statements in core/runtime.py, so they hold things the models do not declare: columns
    such as trainee_assessments.is_completed, indexes such as idx_assessment_configs_course_id,
    and ON DELETE CASCADE foreign keys. Autogenerate reads that drift as "drop it", which would
    delete data or slow queries. A drop must be written by hand, deliberately, in its own revision.
    The matching re-create in downgrade() is removed too, so the revision stays symmetric.
    """
    if not directives:
        return
    script = directives[0]
    upgrade_ops = getattr(script, "upgrade_ops", None)
    downgrade_ops = getattr(script, "downgrade_ops", None)
    if upgrade_ops is None:
        return

    removed: Set[_OpKey] = set()
    upgrade_ops.ops = _without_drops(upgrade_ops.ops, removed)
    if downgrade_ops is not None:
        downgrade_ops.ops = _without_restores(downgrade_ops.ops, removed)
    for kind, name in sorted(removed, key=lambda key: (key[0], str(key[1]))):
        logger.warning(
            "Autogenerate proposed dropping %s %s; left out. Write it by hand if it is intended.",
            kind,
            name,
        )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m core.schema_migrations",
        description="Apply or inspect Smart Tutor's versioned database migrations.",
    )
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("current", help="show the revision the database is at")
    actions.add_parser("history", help="list every revision")
    upgrade = actions.add_parser("upgrade", help="apply revisions up to a target")
    upgrade.add_argument("revision", nargs="?", default="head")
    downgrade = actions.add_parser("downgrade", help="revert revisions down to a target")
    downgrade.add_argument("revision")
    args = parser.parse_args(argv)

    cfg = alembic_config()
    if args.action == "history":
        command.history(cfg)
        return 0

    # The application's engine: the same credentials, Secrets Manager lookup and RDS Proxy
    # handling as the app. (The core package booted the runtime when this module was imported.)
    from core.runtime import apply_legacy_schema_steps, engine
    from core.schema_lock import schema_migration_lock

    if args.action == "current":
        with engine.connect() as connection:
            cfg.attributes["connection"] = connection
            command.current(cfg)
        return 0

    with schema_migration_lock(engine, _CLI_LOCK_WAIT_SECONDS) as acquired:
        if not acquired:
            logger.error("Another process is applying schema changes; try again shortly.")
            return 1
        # The same full sequence startup runs, so this command can replace startup migrations.
        # Unlike startup it stops here on failure: a deploy step must fail loudly, not half-apply.
        if args.action == "upgrade" and not apply_legacy_schema_steps():
            logger.error("A legacy schema step failed; versioned migrations were not applied.")
            return 1
        _run_revisions(engine, args.action, args.revision)
    return 0


if __name__ == "__main__":
    sys.exit(main())
