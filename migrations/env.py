"""Alembic environment. Runs migrations synchronously against SQLite.

We read DATABASE_URL from the environment (the same value the app uses) and
strip the async driver suffix so Alembic can use a plain sync engine.
"""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import engine_from_config, pool

from bot.models import Base

config = context.config

# Prefer the runtime DATABASE_URL; fall back to alembic.ini's placeholder.
_db_url = os.environ.get("DATABASE_URL")
if _db_url:
    # Alembic migrations run synchronously: sqlite+aiosqlite -> sqlite.
    _db_url = _db_url.replace("+aiosqlite", "")
    config.set_main_option("sqlalchemy.url", _db_url)


def _ensure_sqlite_dir(url: str) -> None:
    # On a fresh volume the SQLite directory may not exist yet; create it so the
    # entrypoint's `alembic upgrade head` succeeds on first run.
    marker = "sqlite:///"
    if url.startswith(marker):
        path = "/" + url[len(marker):].lstrip("/")
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)


_ensure_sqlite_dir(config.get_main_option("sqlalchemy.url") or "")

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,  # SQLite-friendly ALTERs
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
