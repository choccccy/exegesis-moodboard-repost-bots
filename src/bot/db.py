"""Async database engine + session factory (SQLite via aiosqlite)."""

from __future__ import annotations

import asyncio
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

# SQLite is single-writer. Serialize all session_scope calls within this
# process so concurrent Discord events and the scheduler never race for
# the write lock. An asyncio.Lock is cooperative (no busy-spin) and safe
# because the bot is a single asyncio process.
_db_lock: asyncio.Lock | None = None


def _sqlite_path(database_url: str) -> str | None:
    """Extract the on-disk path from a sqlite+aiosqlite URL, if applicable."""
    marker = "sqlite+aiosqlite:///"
    if database_url.startswith(marker):
        return "/" + database_url[len(marker):].lstrip("/")
    return None


def init_engine(database_url: str) -> None:
    """Create the global engine/sessionmaker. Ensures the sqlite dir exists."""
    global _engine, _sessionmaker, _db_lock
    path = _sqlite_path(database_url)
    if path:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    _engine = create_async_engine(database_url, future=True)
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    if database_url.startswith("sqlite"):
        _db_lock = asyncio.Lock()


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional session context: commits on success, rolls back on error."""
    if _sessionmaker is None:
        raise RuntimeError("init_engine() must be called before opening a session")
    lock = _db_lock
    async with (lock if lock is not None else _nullcontext()):
        async with _sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise


@asynccontextmanager
async def _nullcontext():
    yield


async def dispose_engine() -> None:
    if _engine is not None:
        await _engine.dispose()
