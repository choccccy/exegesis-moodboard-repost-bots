"""Tests for handle_reaction_removed."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from sqlalchemy import select

from bot.config import BoardConfig
from bot.discord_ingest.service import handle_reaction_removed
from bot.models import PublishAttempt, Submission
from bot.state import SubmissionState

from conftest import make_submission


def _settings(channel_id: int = 100, curator_user_ids: list[int] | None = None) -> MagicMock:
    s = MagicMock()
    cfg = BoardConfig(
        name="robots",
        discord_guild_id=1,
        discord_channel_id=channel_id,
        require_graphic_classification=False,
        curator_user_ids=curator_user_ids or [42],
    )
    s.board_for_channel.return_value = cfg
    s.trigger_emoji = "🦋"
    s.attachments_dir = "/tmp/test-attachments"
    return s


def _channel(user_id: int = 42, is_curator: bool = True) -> MagicMock:
    # channel.guild.fetch_member used in _curator_authorized
    member = MagicMock()
    member.roles = []
    guild = MagicMock()
    guild.get_member.return_value = None
    guild.fetch_member = AsyncMock(return_value=member)
    channel = MagicMock()
    channel.guild = guild
    channel.send = AsyncMock()
    return channel


def _thread_mock() -> MagicMock:
    t = MagicMock(spec=discord.Thread)
    t.id = 500
    t.archived = False
    t.send = AsyncMock()
    t.edit = AsyncMock()
    t.guild = MagicMock()
    return t


@patch("bot.discord_ingest.service.remove_submission_dir")
@patch("bot.discord_ingest.service._archive_thread", new_callable=AsyncMock)
@patch("bot.discord_ingest.service._resolve_thread_by_id", new_callable=AsyncMock)
async def test_deletion_happy_path(mock_resolve_thread, mock_archive, mock_remove, session, board):
    """Curator removes 🦋: submission is deleted from DB."""
    sub = make_submission(board, source_discord_message_id=10)
    session.add(sub)
    await session.flush()
    sub_id = sub.id

    mock_resolve_thread.return_value = None  # no thread
    channel = _channel()
    settings = _settings(channel_id=board.discord_channel_id, curator_user_ids=[42])

    await handle_reaction_removed(
        session,
        settings=settings,
        channel=channel,
        channel_id=board.discord_channel_id,
        message_id=10,
        user_id=42,
    )

    remaining = await session.scalar(select(Submission).where(Submission.id == sub_id))
    assert remaining is None


@patch("bot.discord_ingest.service.remove_submission_dir")
@patch("bot.discord_ingest.service._archive_thread", new_callable=AsyncMock)
@patch("bot.discord_ingest.service._resolve_thread_by_id", new_callable=AsyncMock)
async def test_non_curator_cannot_remove(mock_resolve_thread, mock_archive, mock_remove, session, board):
    """Non-curator removal leaves submission untouched."""
    sub = make_submission(board, source_discord_message_id=11)
    session.add(sub)
    await session.flush()
    sub_id = sub.id

    channel = _channel()
    # Curator user_ids does NOT include user 999
    settings = _settings(channel_id=board.discord_channel_id, curator_user_ids=[42])

    await handle_reaction_removed(
        session,
        settings=settings,
        channel=channel,
        channel_id=board.discord_channel_id,
        message_id=11,
        user_id=999,  # not a curator
    )

    remaining = await session.scalar(select(Submission).where(Submission.id == sub_id))
    assert remaining is not None


async def test_no_submission_returns_gracefully(session, board):
    """If no submission exists for that message, returns without error."""
    channel = _channel()
    settings = _settings(channel_id=board.discord_channel_id, curator_user_ids=[42])

    # Should not raise
    await handle_reaction_removed(
        session,
        settings=settings,
        channel=channel,
        channel_id=board.discord_channel_id,
        message_id=99999,
        user_id=42,
    )


@patch("bot.discord_ingest.service.remove_submission_dir")
@patch("bot.discord_ingest.service._archive_thread", new_callable=AsyncMock)
@patch("bot.discord_ingest.service._resolve_thread_by_id", new_callable=AsyncMock)
async def test_published_submission_not_deleted(mock_resolve_thread, mock_archive, mock_remove, session, board):
    """Already-published submission is blocked from deletion; submission remains in DB."""
    sub = make_submission(board, state=SubmissionState.PUBLISHED.value, source_discord_message_id=12)
    sub.thread_id = 500
    session.add(sub)
    await session.flush()

    attempt = PublishAttempt(
        submission_id=sub.id,
        success=True,
        bsky_url="https://bsky.app/profile/x/post/abc",
    )
    session.add(attempt)
    await session.flush()

    thread = _thread_mock()
    mock_resolve_thread.return_value = thread
    channel = _channel()
    settings = _settings(channel_id=board.discord_channel_id, curator_user_ids=[42])

    await handle_reaction_removed(
        session,
        settings=settings,
        channel=channel,
        channel_id=board.discord_channel_id,
        message_id=12,
        user_id=42,
    )

    remaining = await session.scalar(select(Submission).where(Submission.id == sub.id))
    assert remaining is not None
    # Thread should have received a "cannot remove published" notice
    thread.send.assert_called_once()


@patch("bot.discord_ingest.service.remove_submission_dir")
@patch("bot.discord_ingest.service._archive_thread", new_callable=AsyncMock)
@patch("bot.discord_ingest.service._resolve_thread_by_id", new_callable=AsyncMock)
async def test_thread_notified_on_deletion(mock_resolve_thread, mock_archive, mock_remove, session, board):
    """When a thread exists, it receives a removal notice."""
    sub = make_submission(board, source_discord_message_id=13)
    sub.thread_id = 600
    session.add(sub)
    await session.flush()

    thread = _thread_mock()
    mock_resolve_thread.return_value = thread
    channel = _channel()
    settings = _settings(channel_id=board.discord_channel_id, curator_user_ids=[42])

    await handle_reaction_removed(
        session,
        settings=settings,
        channel=channel,
        channel_id=board.discord_channel_id,
        message_id=13,
        user_id=42,
    )

    thread.send.assert_called()
    mock_archive.assert_called_once()


async def test_unknown_channel_returns_gracefully(session, board):
    """If the channel isn't a watched board channel, returns without error."""
    channel = _channel()
    settings = _settings(channel_id=board.discord_channel_id)

    await handle_reaction_removed(
        session,
        settings=settings,
        channel=channel,
        channel_id=9999,  # not a known channel
        message_id=1,
        user_id=42,
    )
