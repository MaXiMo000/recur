"""Alembic environment.

Migrations run as the OWNER role, not the application role: creating tables,
policies and roles needs privileges the app deliberately does not have. That
split is the point -- if the running app could ALTER its own RLS policies, the
policies would not be a boundary.
"""

from alembic import context
from sqlalchemy import create_engine

from app import db as recur_db

config = context.config
target_metadata = None


def _sqlalchemy_url() -> str:
    """SQLAlchemy picks psycopg2 for a bare postgresql:// URL. This project
    runs on psycopg 3, so the dialect has to name the driver explicitly."""
    dsn = recur_db.OWNER_DSN
    if dsn.startswith("postgresql://"):
        return "postgresql+psycopg://" + dsn[len("postgresql://"):]
    return dsn


def run_migrations_offline() -> None:
    context.configure(url=_sqlalchemy_url(), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_sqlalchemy_url(), future=True)
    with engine.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
