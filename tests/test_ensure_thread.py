"""Tests for _ensure_thread: SubmissionThread DB persistence logic.

This is the platform-agnostic core — creating, reusing, and updating the
per-submission thread mapping — that will move to the new ingest layer during
the platform-agnostic refactor.
"""

from __future__ import annotations

import discord
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select

from bot.discord_ingest.service import _ensure_thread
from bot.models import SubmissionThread
from bot.state import SubmissionState

from conftest import make_submission


PATCH_ANCHOR = "bot.discord_ingest.service._post_thread_anchor"
PATCH_UNARCHIVE = "bot.discord_ingest.service._unarchive_thread"
PATCH_TITLE = "bot.discord_ingest.service._derive_thread_title"
PATCH_RESOLVE = "bot.discord_ingest.service._resolve_thread"


def _message(channel_id: int, msg_id: int = 42) -> MagicMock:
    msg = MagicMock(spec=discord.Message)
    msg.id = msg_id
    msg.embeds = []
    msg.guild = MagicMock()
    msg.guild.get_thread = MagicMock(return_value=None)
    msg.guild.fetch_channel = AsyncMock(side_effect=discord.NotFound(MagicMock(), "not found"))
    msg.channel = MagicMock()
    msg.channel.create_thread = AsyncMock()
    msg.forward = AsyncMock()
    return msg


def _settings():
    s = MagicMock()
    s.board_for_channel.return_value = None
    s.dashboard_url = None
    return s


def _thread(thread_id: int) -> MagicMock:
    t = MagicMock(spec=discord.Thread)
    t.id = thread_id
    t.archived = False
    t.send = AsyncMock(return_value=MagicMock(id=9999, add_reaction=AsyncMock()))
    t.edit = AsyncMock()
    return t


# ---------------------------------------------------------------------------
# New thread creation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ensure_thread_creates_new_thread(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value, source_discord_message_id=42)
    session.add(sub)
    await session.flush()

    new_thread = _thread(500)
    msg = _message(channel_id=board.discord_channel_id, msg_id=42)
    msg.channel.create_thread.return_value = new_thread

    with patch(PATCH_ANCHOR, new_callable=AsyncMock), \
         patch(PATCH_TITLE, new_callable=AsyncMock, return_value="Cool Title"):
        result, is_new = await _ensure_thread(session, _settings(), msg, sub, post_anchor=True)

    assert is_new is True
    assert result is new_thread
    assert sub.thread_id == 500

    mapping = await session.scalar(
        select(SubmissionThread).where(
            SubmissionThread.source_discord_message_id == 42
        )
    )
    assert mapping is not None
    assert mapping.thread_id == 500
    assert mapping.board_id == board.id


@pytest.mark.asyncio
async def test_ensure_thread_new_passes_title_to_create_thread(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value, source_discord_message_id=142)
    session.add(sub)
    await session.flush()

    new_thread = _thread(501)
    msg = _message(channel_id=board.discord_channel_id, msg_id=142)
    msg.channel.create_thread.return_value = new_thread

    with patch(PATCH_ANCHOR, new_callable=AsyncMock), \
         patch(PATCH_TITLE, new_callable=AsyncMock, return_value="My Thread Title"):
        await _ensure_thread(session, _settings(), msg, sub)

    msg.channel.create_thread.assert_awaited_once()
    assert msg.channel.create_thread.call_args.kwargs.get("name") == "My Thread Title"


# ---------------------------------------------------------------------------
# Existing mapping — thread found
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ensure_thread_reuses_existing_mapping(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value, source_discord_message_id=43)
    session.add(sub)
    await session.flush()

    existing = _thread(600)
    session.add(SubmissionThread(
        board_id=board.id,
        source_discord_message_id=43,
        thread_id=600,
    ))
    await session.flush()

    msg = _message(channel_id=board.discord_channel_id, msg_id=43)

    with patch(PATCH_ANCHOR, new_callable=AsyncMock) as mock_anchor, \
         patch(PATCH_UNARCHIVE, new_callable=AsyncMock), \
         patch(PATCH_TITLE, new_callable=AsyncMock, return_value="Title"), \
         patch(PATCH_RESOLVE, new_callable=AsyncMock, return_value=existing):
        result, is_new = await _ensure_thread(session, _settings(), msg, sub, post_anchor=True)

    assert is_new is False
    assert result is existing
    assert sub.thread_id == 600
    msg.channel.create_thread.assert_not_called()
    mock_anchor.assert_awaited_once()


@pytest.mark.asyncio
async def test_ensure_thread_no_anchor_on_rescan(session, board):
    """When post_anchor=False (re-scan of existing submission), anchor is not re-posted."""
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value, source_discord_message_id=44)
    session.add(sub)
    await session.flush()

    existing = _thread(700)
    session.add(SubmissionThread(
        board_id=board.id,
        source_discord_message_id=44,
        thread_id=700,
    ))
    await session.flush()

    msg = _message(channel_id=board.discord_channel_id, msg_id=44)

    with patch(PATCH_ANCHOR, new_callable=AsyncMock) as mock_anchor, \
         patch(PATCH_UNARCHIVE, new_callable=AsyncMock) as mock_unarchive, \
         patch(PATCH_TITLE, new_callable=AsyncMock, return_value="Title"), \
         patch(PATCH_RESOLVE, new_callable=AsyncMock, return_value=existing):
        result, is_new = await _ensure_thread(session, _settings(), msg, sub, post_anchor=False)

    assert is_new is False
    mock_anchor.assert_not_awaited()
    mock_unarchive.assert_not_awaited()


# ---------------------------------------------------------------------------
# Existing mapping — thread gone (stale)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ensure_thread_stale_mapping_creates_new_and_updates(session, board):
    """Mapping exists but the thread is gone — creates a new thread, updates the mapping in-place."""
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value, source_discord_message_id=45)
    session.add(sub)
    await session.flush()

    stale = SubmissionThread(
        board_id=board.id,
        source_discord_message_id=45,
        thread_id=800,
    )
    session.add(stale)
    await session.flush()

    new_thread = _thread(801)
    msg = _message(channel_id=board.discord_channel_id, msg_id=45)
    msg.channel.create_thread.return_value = new_thread

    with patch(PATCH_ANCHOR, new_callable=AsyncMock), \
         patch(PATCH_TITLE, new_callable=AsyncMock, return_value="Title"), \
         patch(PATCH_RESOLVE, new_callable=AsyncMock, return_value=None):
        result, is_new = await _ensure_thread(session, _settings(), msg, sub, post_anchor=True)

    assert is_new is True
    assert result is new_thread
    assert stale.thread_id == 801  # in-place update, not a second row


@pytest.mark.asyncio
async def test_ensure_thread_stale_does_not_create_duplicate_mapping(session, board):
    """Only one SubmissionThread row should exist after recovering from a stale mapping."""
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value, source_discord_message_id=46)
    session.add(sub)
    await session.flush()

    session.add(SubmissionThread(board_id=board.id, source_discord_message_id=46, thread_id=900))
    await session.flush()

    msg = _message(channel_id=board.discord_channel_id, msg_id=46)
    msg.channel.create_thread.return_value = _thread(901)

    with patch(PATCH_ANCHOR, new_callable=AsyncMock), \
         patch(PATCH_TITLE, new_callable=AsyncMock, return_value="Title"), \
         patch(PATCH_RESOLVE, new_callable=AsyncMock, return_value=None):
        await _ensure_thread(session, _settings(), msg, sub)

    rows = list(await session.scalars(
        select(SubmissionThread).where(SubmissionThread.source_discord_message_id == 46)
    ))
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Timeout / error paths
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ensure_thread_timeout_returns_none(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value, source_discord_message_id=47)
    session.add(sub)
    await session.flush()

    msg = _message(channel_id=board.discord_channel_id, msg_id=47)
    msg.channel.create_thread.side_effect = TimeoutError

    with patch(PATCH_TITLE, new_callable=AsyncMock, return_value="Title"):
        result, is_new = await _ensure_thread(session, _settings(), msg, sub)

    assert result is None
    assert is_new is False


@pytest.mark.asyncio
async def test_ensure_thread_http_exception_returns_none(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value, source_discord_message_id=48)
    session.add(sub)
    await session.flush()

    msg = _message(channel_id=board.discord_channel_id, msg_id=48)
    msg.channel.create_thread.side_effect = discord.HTTPException(MagicMock(), "rate limited")

    with patch(PATCH_TITLE, new_callable=AsyncMock, return_value="Title"):
        result, is_new = await _ensure_thread(session, _settings(), msg, sub)

    assert result is None
    assert is_new is False
