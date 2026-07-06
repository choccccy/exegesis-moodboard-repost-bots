"""Tests for bot.errors.record_error - persistence and swallow-on-failure."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from sqlalchemy import select

from bot.errors import record_error
from bot.models import BotError


async def test_record_error_writes_row(global_engine):
    try:
        raise ValueError("bad payload")
    except ValueError:
        await record_error("scheduler", "board robots posting")

    async with global_engine.session_scope() as session:
        row = (await session.execute(select(BotError))).scalar_one()
    assert row.source == "scheduler"
    assert row.context == "board robots posting"


async def test_record_error_traceback_contains_exception(global_engine):
    try:
        raise ValueError("bad payload")
    except ValueError:
        await record_error("src", "ctx")

    async with global_engine.session_scope() as session:
        row = (await session.execute(select(BotError))).scalar_one()
    assert "ValueError" in row.traceback
    assert "bad payload" in row.traceback


async def test_record_error_swallows_db_failure(global_engine):
    with patch("bot.errors.session_scope", MagicMock(side_effect=RuntimeError("db down"))):
        try:
            raise ValueError("original")
        except ValueError:
            await record_error("src", "ctx")  # must not propagate
