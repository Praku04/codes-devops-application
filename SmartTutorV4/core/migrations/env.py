"""
Alembic environment for Smart Tutor.

Migrations use the application's own engine, never a URL in alembic.ini, so they connect with the
same credentials, Secrets Manager lookup and RDS Proxy handling as the app and no database
credential is ever written into a config file. At startup and from ``python -m
core.schema_migrations`` the caller passes in a connection it already opened; the bare ``alembic``
command (used to draft revisions) falls back to opening one here.

Logging is left to core.runtime, which configures it on import; calling fileConfig here would
replace the application's handlers.
"""

from alembic import context

from core.schema_migrations import drop_destructive_autogenerate_ops
from models.domain import Base  # importing the models registers every table on Base.metadata

config = context.config
target_metadata = Base.metadata


def _configure_and_run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        process_revision_directives=drop_destructive_autogenerate_ops,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    """Emit SQL for review (``--sql``) without connecting; the dialect is all that is needed."""
    context.configure(
        dialect_name="postgresql",
        target_metadata=target_metadata,
        literal_binds=True,
        process_revision_directives=drop_destructive_autogenerate_ops,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _configure_and_run(connection)
        return

    from core.runtime import engine

    with engine.connect() as own_connection:
        _configure_and_run(own_connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
