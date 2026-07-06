"""Shared fixtures for integration tests."""

from __future__ import annotations

import asyncio
import contextlib
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


class _MockPartial:
    """Fake discord PartialMessage: records edit() calls onto the owning MockDest."""

    def __init__(self, dest: "MockDest", message_id: int):
        self._dest = dest
        self._id = message_id

    async def edit(self, content=None, *, view=None, **kwargs):
        self._dest.edits.append((self._id, content or ""))


class MockDest:
    """Captures messages sent to a Discord channel/thread."""

    def __init__(self):
        self.sent: list[str] = []
        self.edits: list[tuple[int, str]] = []  # (message_id, content) per in-place edit

    async def send(self, content=None, **kwargs):
        self.sent.append(content or "")
        msg = MagicMock()
        msg.id = next(_msg_id)
        msg.add_reaction = AsyncMock()
        return msg

    def get_partial_message(self, message_id: int) -> "_MockPartial":
        return _MockPartial(self, message_id)

    async def archive(self, notice: str) -> None:
        self.sent.append(f"[archive] {notice}")


# ---------------------------------------------------------------------------
# Coverage-suite fixtures: global engine, session binding, loop control, bot
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def global_engine(tmp_path):
    """Point bot.db's module globals at a temp-file DB, restoring them after.

    For code that opens bot.db.session_scope() internally (scheduler bodies,
    errors.record_error, admin scripts, e2e flows). The plain `session` fixture
    never touches these globals, so the two coexist safely.
    """
    import bot.db as db

    saved = (db._engine, db._sessionmaker, db._db_lock)
    db.init_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    async with db._engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield db
    await db.dispose_engine()
    db._engine, db._sessionmaker, db._db_lock = saved


def bound_session_scope(session):
    """Return a session_scope() replacement bound to an existing test session.

    Usage: patch("bot.<module>.session_scope", bound_session_scope(session))
    """

    @contextlib.asynccontextmanager
    async def _scope():
        yield session
        await session.flush()

    return _scope


def make_one_shot_wait_for(stop: asyncio.Event):
    """Fake asyncio.wait_for that runs exactly one loop-body iteration.

    First call closes the stop.wait() coroutine (avoiding 'never awaited'
    warnings), sets the stop event so the loop exits after this iteration,
    and raises TimeoutError to fall through to the loop body.
    """

    async def _fake_wait_for(awaitable, timeout):
        awaitable.close()
        stop.set()
        raise asyncio.TimeoutError

    return _fake_wait_for


def make_test_settings(**overrides):
    """MagicMock(spec=Settings) with the attributes RepostBot and handlers read."""
    from bot.config import BoardConfig, Settings

    board_cfg = BoardConfig(
        name="robots",
        discord_guild_id=1,
        discord_channel_id=100,
        bluesky_handle="robots.exegesis.space",
        tags=["robots"],
    )
    s = MagicMock(spec=Settings)
    s.boards = [board_cfg]
    s.trigger_emoji = "\N{BUTTERFLY}"
    s.catchup_enabled = False
    s.catchup_lookback_hours = 168
    s.catchup_max_messages = 500
    s.data_dir = "/tmp/data"
    s.attachments_dir = "/tmp/attachments"
    s.storage_min_free_mb = 100
    s.youtube_api_key = None
    s.board_for_channel = lambda cid: board_cfg if cid == 100 else None
    s.bsky_password_for = lambda name: "app-password"
    for key, value in overrides.items():
        setattr(s, key, value)
    return s


@pytest.fixture
def repost_bot():
    """A real RepostBot instance with no gateway connection."""
    from bot.discord_ingest.client import RepostBot

    return RepostBot(make_test_settings())


def make_reaction_payload(*, emoji="\N{BUTTERFLY}", channel_id=100, message_id=555,
                          user_id=999, guild_id=1):
    """RawReactionActionEvent-shaped mock for on_raw_reaction_add/remove."""
    import discord

    payload = MagicMock(spec=discord.RawReactionActionEvent)
    payload.emoji = MagicMock()
    payload.emoji.__str__ = lambda self: emoji
    payload.emoji.name = emoji
    payload.channel_id = channel_id
    payload.message_id = message_id
    payload.user_id = user_id
    payload.guild_id = guild_id
    payload.member = MagicMock()
    payload.member.bot = False
    return payload


def make_interaction(*, custom_id="confirm:1", user_id=999, channel_id=100):
    """Component-interaction mock for on_interaction routing tests."""
    import discord

    interaction = MagicMock(spec=discord.Interaction)
    interaction.type = discord.InteractionType.component
    interaction.data = {"custom_id": custom_id}
    interaction.user = MagicMock()
    interaction.user.id = user_id
    interaction.channel = MagicMock()
    interaction.channel.id = channel_id
    interaction.channel.send = AsyncMock()
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction
