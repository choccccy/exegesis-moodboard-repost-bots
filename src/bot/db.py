"""Async database engine + session factory (SQLite via aiosqlite)."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _sqlite_path(database_url: str) -> str | None:
    """Extract the on-disk path from a sqlite+aiosqlite URL, if applicable."""
    marker = "sqlite+aiosqlite:///"
    if database_url.startswith(marker):
        return "/" + database_url[len(marker):].lstrip("/")
    return None


def init_engine(database_url: str) -> None:
    """Create the global engine/sessionmaker. Ensures the sqlite dir exists."""
    global _engine, _sessionmaker
    path = _sqlite_path(database_url)
    if path:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    _engine = create_async_engine(
        database_url,
        future=True,
        connect_args={"timeout": 30} if database_url.startswith("sqlite") else {},
    )
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)

    # Enable WAL mode so concurrent readers don't block writers (and vice-versa).
    # Must use cursor() — direct conn.execute() is a no-op on aiosqlite's sync adapter.
    if database_url.startswith("sqlite"):
        from sqlalchemy import event

        @event.listens_for(_engine.sync_engine, "connect")
        def _set_wal(dbapi_conn, _):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional session context: commits on success, rolls back on error."""
    if _sessionmaker is None:
        raise RuntimeError("init_engine() must be called before opening a session")
    async with _sessionmaker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    if _engine is not None:
        await _engine.dispose()
