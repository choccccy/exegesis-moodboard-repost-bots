"""Tests for bot.db - engine init, sqlite path parsing, session_scope semantics."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select

import bot.db as db
from bot.models import Board


def _save_globals():
    return (db._engine, db._sessionmaker, db._db_lock)


def _restore_globals(saved):
    db._engine, db._sessionmaker, db._db_lock = saved


# ---------------------------------------------------------------------------
# _sqlite_path
# ---------------------------------------------------------------------------


def test_sqlite_path_relative_form_gets_leading_slash():
    # Three-slash form: implementation prepends "/" to whatever follows.
    assert db._sqlite_path("sqlite+aiosqlite:///data/bot.db") == "/data/bot.db"


def test_sqlite_path_absolute_form():
    # Four-slash form: extra slashes are collapsed to a single leading one.
    assert db._sqlite_path("sqlite+aiosqlite:////var/lib/bot.db") == "/var/lib/bot.db"


def test_sqlite_path_non_sqlite_url_returns_none():
    assert db._sqlite_path("postgresql+asyncpg://user:pw@host/dbname") is None
    assert db._sqlite_path("sqlite:///plain-driver.db") is None


# ---------------------------------------------------------------------------
# init_engine
# ---------------------------------------------------------------------------


async def test_init_engine_creates_parent_directory(tmp_path):
    saved = _save_globals()
    target = tmp_path / "nested" / "dirs" / "bot.db"
    try:
        db.init_engine(f"sqlite+aiosqlite:///{target}")
        assert target.parent.is_dir()
        assert db._engine is not None
        assert db._sessionmaker is not None
        await db.dispose_engine()
    finally:
        _restore_globals(saved)


async def test_init_engine_sets_asyncio_lock_for_sqlite(global_engine):
    assert isinstance(global_engine._db_lock, asyncio.Lock)


def test_init_engine_no_lock_for_non_sqlite():
    saved = _save_globals()
    try:
        db._db_lock = None
        with patch("bot.db.create_async_engine", return_value=MagicMock()), \
             patch("bot.db.async_sessionmaker", return_value=MagicMock()):
            db.init_engine("postgresql+asyncpg://user:pw@host/dbname")
        assert db._db_lock is None
    finally:
        _restore_globals(saved)


# ---------------------------------------------------------------------------
# session_scope
# ---------------------------------------------------------------------------


async def test_session_scope_commits_on_clean_exit(global_engine):
    async with db.session_scope() as session:
        session.add(Board(name="commit-me", discord_guild_id=1, discord_channel_id=101))

    async with db.session_scope() as session:
        row = (await session.execute(
            select(Board).where(Board.name == "commit-me")
        )).scalar_one_or_none()
    assert row is not None
    assert row.discord_channel_id == 101


async def test_session_scope_rolls_back_on_exception(global_engine):
    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        async with db.session_scope() as session:
            session.add(Board(name="roll-me-back", discord_guild_id=1, discord_channel_id=102))
            await session.flush()
            raise Boom

    async with db.session_scope() as session:
        row = (await session.execute(
            select(Board).where(Board.name == "roll-me-back")
        )).scalar_one_or_none()
    assert row is None


async def test_session_scope_without_lock_uses_nullcontext(global_engine):
    # Non-sqlite deployments have no _db_lock; session_scope must still work.
    global_engine._db_lock = None
    async with db.session_scope() as session:
        session.add(Board(name="lockless", discord_guild_id=1, discord_channel_id=103))

    async with db.session_scope() as session:
        row = (await session.execute(
            select(Board).where(Board.name == "lockless")
        )).scalar_one_or_none()
    assert row is not None


async def test_session_scope_raises_when_engine_not_initialized():
    saved = _save_globals()
    try:
        db._engine = None
        db._sessionmaker = None
        db._db_lock = None
        with pytest.raises(RuntimeError, match="init_engine"):
            async with db.session_scope():
                pass  # pragma: no cover
    finally:
        _restore_globals(saved)


# ---------------------------------------------------------------------------
# dispose_engine
# ---------------------------------------------------------------------------


async def test_dispose_engine_noop_when_engine_is_none():
    saved = _save_globals()
    try:
        db._engine = None
        await db.dispose_engine()  # must not raise
    finally:
        _restore_globals(saved)
