"""
Cross-process serialisation for schema changes, using a Postgres advisory lock.

Every container runs several uvicorn workers and a rolling ECS deploy starts containers side by
side, so several processes reach the startup schema code at the same moment. Without a lock they
ran the same CREATE TABLE / ALTER TABLE statements concurrently: on an empty database all but one
failed with a UniqueViolation on pg_type, and on an existing one a worker could fail with
DuplicateColumn. Both errors aborted the rest of that worker's schema steps. With the lock the
processes take turns - the first applies the changes, the others find nothing left to do.

Imports nothing from the application but log_safe, so core.runtime can call it without an import
cycle. (Importing it still boots the runtime, as any core.* import does via core/__init__.py.)
"""

import logging
import time
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import text
from sqlalchemy.engine import Engine

from utils.security_helpers import log_safe

logger = logging.getLogger(__name__)

# Fixed and arbitrary: what matters is that every Smart Tutor process contends for the same key,
# and that no other application sharing the database happens to use it.
SCHEMA_MIGRATION_LOCK_KEY = 5_734_119_026

_POLL_INTERVAL_SECONDS = 0.5


@contextmanager
def schema_migration_lock(engine: Engine, wait_seconds: float) -> Iterator[bool]:
    """
    Hold the schema advisory lock for the duration of the block.

    Yields True once the lock is held, or False if another process still held it after
    ``wait_seconds``; the caller decides what to do then, so a stuck holder can never block
    startup indefinitely.

    A session-level lock is used, not pg_advisory_xact_lock, because the schema steps commit
    several times and a transaction-level lock would be released at the first commit. The lock
    lives on its own connection, so the schema steps can keep opening theirs. Behind RDS Proxy a
    session lock pins that one connection, which is released when it closes below.
    """
    conn = engine.connect()
    acquired = False
    try:
        deadline = time.monotonic() + max(0.0, wait_seconds)
        while True:
            acquired = bool(
                conn.execute(
                    text("SELECT pg_try_advisory_lock(:key)"),
                    {"key": SCHEMA_MIGRATION_LOCK_KEY},
                ).scalar()
            )
            # Close the implicit transaction: the session lock survives it, and an idle-in-
            # transaction connection could be cut off by idle_in_transaction_session_timeout.
            conn.commit()
            if acquired or time.monotonic() >= deadline:
                break
            time.sleep(_POLL_INTERVAL_SECONDS)
        yield acquired
    finally:
        try:
            if acquired:
                conn.execute(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": SCHEMA_MIGRATION_LOCK_KEY},
                )
                conn.commit()
        except Exception as exc:
            # Never hand a pooled connection that may still hold the lock back to the pool:
            # discarding it ends the session, and Postgres releases the lock with it.
            logger.warning("Could not release the schema lock cleanly: %s", log_safe(exc))
            conn.invalidate()
        finally:
            conn.close()
