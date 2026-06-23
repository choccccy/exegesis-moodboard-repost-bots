"""Integration tests for the queue scheduler (_fire_board).

Verifies that _fire_board correctly resolves SubmissionThread rows and calls
publish_queued_submission, using an in-memory SQLite DB and mocked publish.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.models import PublishAttempt, SubmissionThread
from bot.scheduler import _fire_board
from bot.state import SubmissionState

from conftest import make_submission


QUEUED = SubmissionState.QUEUED.value

_NOW = datetime(2026, 6, 23, 18, 0, 0, tzinfo=timezone.utc)
_FRESH_CUTOFF = _NOW - timedelta(hours=72)
_MT_MIDNIGHT = _NOW.replace(hour=0, minute=0, second=0)


class _FakeSettings:
    queue_fresh_daily_cap = 6
    queue_backlog_daily_cap = 3


def _board_cfg(board):
    cfg = MagicMock()
    cfg.name = board.name
    cfg.discord_channel_id = board.discord_channel_id
    cfg.bluesky_handle = f"@{board.name}.bsky.social"
    return cfg


def _fake_bot():
    bot = MagicMock()
    bot.fetch_channel = AsyncMock(return_value=MagicMock())
    return bot


# ---------------------------------------------------------------------------
# Core publish path
# ---------------------------------------------------------------------------

async def test_fire_board_calls_publish(session, board):
    sub = make_submission(board, state=QUEUED, source_discord_message_id=42)
    session.add(sub)
    await session.flush()

    with patch("bot.scheduler.ingest_service.publish_queued_submission", new_callable=AsyncMock) as mock_pub:
        await _fire_board(session, _fake_bot(), _FakeSettings(), _board_cfg(board), _FRESH_CUTOFF, _MT_MIDNIGHT)

    mock_pub.assert_awaited_once()
    # publish_queued_submission(session, settings, submission, destination) — 4 args
    _, _, submission_arg, _ = mock_pub.await_args.args
    assert submission_arg.id == sub.id


async def test_fire_board_no_thread_passes_none_destination(session, board):
    sub = make_submission(board, state=QUEUED, source_discord_message_id=99)
    session.add(sub)
    await session.flush()

    with patch("bot.scheduler.ingest_service.publish_queued_submission", new_callable=AsyncMock) as mock_pub:
        await _fire_board(session, _fake_bot(), _FakeSettings(), _board_cfg(board), _FRESH_CUTOFF, _MT_MIDNIGHT)

    _, _, _, destination = mock_pub.await_args.args
    assert destination is None


async def test_fire_board_with_thread_fetches_channel(session, board):
    sub = make_submission(board, state=QUEUED, source_discord_message_id=55)
    session.add(sub)
    await session.flush()

    thread = SubmissionThread(
        board_id=board.id,
        source_discord_message_id=55,
        thread_id=9999,
    )
    session.add(thread)
    await session.flush()

    fake_channel = MagicMock()
    bot = _fake_bot()
    bot.fetch_channel = AsyncMock(return_value=fake_channel)

    with patch("bot.scheduler.ingest_service.publish_queued_submission", new_callable=AsyncMock) as mock_pub:
        await _fire_board(session, bot, _FakeSettings(), _board_cfg(board), _FRESH_CUTOFF, _MT_MIDNIGHT)

    bot.fetch_channel.assert_awaited_once_with(9999)
    _, _, _, destination = mock_pub.await_args.args
    assert destination is fake_channel


async def test_fire_board_thread_fetch_failure_still_publishes(session, board):
    sub = make_submission(board, state=QUEUED, source_discord_message_id=77)
    session.add(sub)
    await session.flush()

    thread = SubmissionThread(board_id=board.id, source_discord_message_id=77, thread_id=8888)
    session.add(thread)
    await session.flush()

    bot = _fake_bot()
    bot.fetch_channel = AsyncMock(side_effect=Exception("unknown channel"))

    with patch("bot.scheduler.ingest_service.publish_queued_submission", new_callable=AsyncMock) as mock_pub:
        await _fire_board(session, bot, _FakeSettings(), _board_cfg(board), _FRESH_CUTOFF, _MT_MIDNIGHT)

    mock_pub.assert_awaited_once()
    _, _, _, destination = mock_pub.await_args.args
    assert destination is None


# ---------------------------------------------------------------------------
# Skip conditions
# ---------------------------------------------------------------------------

async def test_fire_board_nothing_queued_skips_publish(session, board):
    with patch("bot.scheduler.ingest_service.publish_queued_submission", new_callable=AsyncMock) as mock_pub:
        await _fire_board(session, _fake_bot(), _FakeSettings(), _board_cfg(board), _FRESH_CUTOFF, _MT_MIDNIGHT)

    mock_pub.assert_not_awaited()


async def test_fire_board_at_daily_cap_skips_publish(session, board):
    sub = make_submission(board, state=QUEUED, source_discord_message_id=11)
    session.add(sub)
    await session.flush()

    # 6 successful publishes today = hits fresh cap
    for i in range(6):
        attempt = PublishAttempt(
            submission_id=sub.id,
            success=True,
            attempted_at=_NOW - timedelta(minutes=i + 1),
        )
        session.add(attempt)
    await session.flush()

    with patch("bot.scheduler.ingest_service.publish_queued_submission", new_callable=AsyncMock) as mock_pub:
        await _fire_board(session, _fake_bot(), _FakeSettings(), _board_cfg(board), _FRESH_CUTOFF, _MT_MIDNIGHT)

    mock_pub.assert_not_awaited()


async def test_fire_board_unknown_board_skips_publish(session, board):
    cfg = _board_cfg(board)
    cfg.discord_channel_id = 99999  # no DB row for this channel

    with patch("bot.scheduler.ingest_service.publish_queued_submission", new_callable=AsyncMock) as mock_pub:
        await _fire_board(session, _fake_bot(), _FakeSettings(), cfg, _FRESH_CUTOFF, _MT_MIDNIGHT)

    mock_pub.assert_not_awaited()
