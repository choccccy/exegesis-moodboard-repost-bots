"""Shared fixtures for integration tests."""

from __future__ import annotations

import itertools
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.models import Base, Board, Submission
from bot.state import GraphicStatus, SubmissionState

_msg_id = itertools.count(10_000)


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as s:
        yield s
    await engine.dispose()


@pytest_asyncio.fixture
async def board(session):
    b = Board(name="robots", discord_guild_id=1, discord_channel_id=100)
    session.add(b)
    await session.flush()
    return b


def make_submission(board: Board, state: str = SubmissionState.INTENT_SUBMITTED.value, **kw) -> Submission:
    defaults = dict(
        board_id=board.id,
        source_discord_message_id=1,
        channel_id=board.discord_channel_id,
        author_id=999,
        author_display="test_user",
        state=state,
        graphic_status=GraphicStatus.UNKNOWN.value,
        graphic_classification_required=False,
    )
    defaults.update(kw)
    return Submission(**defaults)


class MockDest:
    """Captures messages sent to a Discord channel/thread."""

    def __init__(self):
        self.sent: list[str] = []

    async def send(self, content=None, **kwargs):
        self.sent.append(content or "")
        msg = MagicMock()
        msg.id = next(_msg_id)
        msg.add_reaction = AsyncMock()
        return msg
