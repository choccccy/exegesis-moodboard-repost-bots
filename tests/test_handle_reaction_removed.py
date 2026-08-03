"""Tests for handle_reaction_removed (🦋 removed on the source post).

The handler is surface-agnostic: it takes a normalized ReactionEvent (whose member
the gateway resolves, since reaction-remove payloads carry none) and a Surface (the
submission's thread), and routes the removal notice + archival through the port.
"""
from __future__ import annotations

from sqlalchemy import select

from bot.config import BoardConfig
from bot.curation.events import ReactionEvent
from bot.curation.handlers import handle_reaction_removed
from bot.models import PublishAttempt, Submission
from bot.state import SubmissionState

from conftest import MockDest, make_submission


def _settings(channel_id: int = 100, curator_user_ids: list[int] | None = None) -> "MagicMock":
    from unittest.mock import MagicMock
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


def _event(user_id: int, message_id: int, *, channel_id: int, member=None):
    return ReactionEvent(user_id=user_id, message_id=message_id, channel_id=channel_id,
                         emoji="🦋", member=member)


async def test_deletion_happy_path(session, board):
    """Curator removes 🦋: submission is deleted from DB."""
    sub = make_submission(board, source_discord_message_id=10)
    session.add(sub)
    await session.flush()
    sub_id = sub.id
    settings = _settings(channel_id=board.discord_channel_id, curator_user_ids=[42])

    await handle_reaction_removed(
        session, _event(42, 10, channel_id=board.discord_channel_id), MockDest(), settings,
    )

    remaining = await session.scalar(select(Submission).where(Submission.id == sub_id))
    assert remaining is None


async def test_non_curator_cannot_remove(session, board):
    """Non-curator removal leaves submission untouched (member=None, not in curator ids)."""
    sub = make_submission(board, source_discord_message_id=11)
    session.add(sub)
    await session.flush()
    sub_id = sub.id
    settings = _settings(channel_id=board.discord_channel_id, curator_user_ids=[42])

    await handle_reaction_removed(
        session, _event(999, 11, channel_id=board.discord_channel_id), MockDest(), settings,
    )

    remaining = await session.scalar(select(Submission).where(Submission.id == sub_id))
    assert remaining is not None


async def test_no_submission_returns_gracefully(session, board):
    """If no submission exists for that message, returns without error."""
    settings = _settings(channel_id=board.discord_channel_id, curator_user_ids=[42])
    await handle_reaction_removed(
        session, _event(42, 99999, channel_id=board.discord_channel_id), MockDest(), settings,
    )


async def test_published_submission_not_deleted(session, board):
    """Already-published submission is blocked from deletion; the thread is told why."""
    sub = make_submission(board, state=SubmissionState.PUBLISHED.value, source_discord_message_id=12)
    sub.thread_id = 500
    session.add(sub)
    await session.flush()
    session.add(PublishAttempt(
        submission_id=sub.id, success=True, bsky_url="https://bsky.app/profile/x/post/abc",
    ))
    await session.flush()
    dest = MockDest()
    settings = _settings(channel_id=board.discord_channel_id, curator_user_ids=[42])

    await handle_reaction_removed(
        session, _event(42, 12, channel_id=board.discord_channel_id), dest, settings,
    )

    remaining = await session.scalar(select(Submission).where(Submission.id == sub.id))
    assert remaining is not None
    assert len(dest.sent) == 1  # cannot-remove-published notice
    assert not dest.archived


async def test_thread_notified_on_deletion(session, board):
    """On deletion the thread receives a removal notice and is archived through the port."""
    sub = make_submission(board, source_discord_message_id=13)
    sub.thread_id = 600
    session.add(sub)
    await session.flush()
    dest = MockDest()
    settings = _settings(channel_id=board.discord_channel_id, curator_user_ids=[42])

    await handle_reaction_removed(
        session, _event(42, 13, channel_id=board.discord_channel_id), dest, settings,
    )

    assert dest.sent  # reaction-removed notice
    assert dest.archived


async def test_unknown_channel_returns_gracefully(session, board):
    """If the channel isn't a watched board channel, returns without error."""
    settings = _settings(channel_id=board.discord_channel_id)
    await handle_reaction_removed(
        session, _event(42, 1, channel_id=9999), MockDest(), settings,
    )
