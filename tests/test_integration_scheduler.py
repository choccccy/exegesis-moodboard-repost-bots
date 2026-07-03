"""Integration tests for the queue scheduler (_fire_board).

Verifies that _fire_board correctly resolves SubmissionThread rows and calls
publish_queued_submission, using an in-memory SQLite DB and mocked publish.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from bot.config import BoardConfig, Settings
from bot.discord_ingest.discord_notifier import DiscordNotifier
from bot.discord_ingest.service import publish_queued_submission
from bot.models import PublishAttempt, SubmissionLink, SubmissionThread
from bot.publish import PublishResult
from bot.scheduler import _fire_board, run_queue_dispatcher
from bot.state import SubmissionState

from conftest import MockDest, make_submission


QUEUED = SubmissionState.QUEUED.value

_NOW = datetime(2026, 6, 23, 18, 0, 0, tzinfo=timezone.utc)
_FRESH_CUTOFF = _NOW - timedelta(hours=72)
_MT_MIDNIGHT = _NOW.replace(hour=0, minute=0, second=0)


class _FakeSettings:
    queue_target_days = 90
    queue_min_daily = 1
    queue_max_daily = 6


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
    # publish_queued_submission(session, settings, submission, destination) - 4 args
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
    assert isinstance(destination, DiscordNotifier)
    assert destination._channel is fake_channel


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

    # 1 queued item → cap = max(1, round(1/90)) = 1.
    # 1 successful publish today meets the cap, so the next tick should skip.
    attempt = PublishAttempt(
        submission_id=sub.id,
        success=True,
        attempted_at=_NOW - timedelta(minutes=1),
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


# ---------------------------------------------------------------------------
# Duplicate-handling regression tests
# ---------------------------------------------------------------------------
#
# Bug: _fire_board returned True on duplicate cleanup (same as a real publish),
# so each scheduler tick could only clear one duplicate from the queue. With many
# old duplicates ahead of real posts, the bot would go hours without posting.
# Fix: _attempt_publish returns None for duplicate cleanups; _fire_board loops
# past them to find the first real submission.

_OK_RESULT = PublishResult(
    success=True,
    at_uri="at://did:plc:test/app.bsky.feed.post/new",
    bsky_url="https://bsky.app/profile/robots.exegesis.space/post/new",
)


def _full_settings(board):
    """Settings that include board_for_channel/bsky_password_for for _attempt_publish."""
    cfg = BoardConfig(
        name=board.name,
        discord_guild_id=board.discord_guild_id,
        discord_channel_id=board.discord_channel_id,
        bluesky_handle=f"@{board.name}.exegesis.space",
        tags=[],
    )
    s = MagicMock()
    s.board_for_channel.return_value = cfg
    s.bsky_password_for.return_value = "app-password"
    s.queue_target_days = 90
    s.queue_min_daily = 1
    s.queue_max_daily = 6
    return s


async def _add_link(session, submission_id, url, canonical_url=None):
    link = SubmissionLink(
        submission_id=submission_id,
        order_index=0,
        raw_url=url,
        canonical_url=canonical_url or url,
        domain_family="other",
    )
    session.add(link)
    await session.flush()
    return link


async def test_fire_board_continues_past_duplicate_to_real_post(session, board):
    """Regression: duplicate cleanups must not consume the scheduler tick.

    When the head of the queue is a duplicate, _fire_board must continue and
    publish the next real submission in the same tick.
    """
    settings = _full_settings(board)

    # Sub A: already published with a real PublishAttempt (yesterday, not counting toward today's cap)
    sub_a = make_submission(board, state=SubmissionState.PUBLISHED.value, source_discord_message_id=1)
    session.add(sub_a)
    await session.flush()
    await _add_link(session, sub_a.id, "https://example.com/already-posted")
    session.add(PublishAttempt(
        submission_id=sub_a.id, success=True, error=None,
        at_uri="at://did/old", bsky_url="https://bsky.app/old",
        attempted_at=_MT_MIDNIGHT - timedelta(hours=1),  # yesterday: doesn't count toward today's cap
    ))
    await session.flush()

    # Sub B: QUEUED duplicate of A (same canonical_url, no prior attempt yet).
    # Explicit created_at ensures B is picked before C by the queue ordering.
    sub_b = make_submission(board, state=QUEUED, source_discord_message_id=2,
                            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    session.add(sub_b)
    await session.flush()
    await _add_link(session, sub_b.id, "https://example.com/already-posted")

    # Sub C: QUEUED, unique URL - the real post we want this tick
    sub_c = make_submission(board, state=QUEUED, source_discord_message_id=3,
                            created_at=datetime(2026, 1, 2, tzinfo=timezone.utc))
    session.add(sub_c)
    await session.flush()
    await _add_link(session, sub_c.id, "https://example.com/new-unique")

    with patch("bot.publish.publish_submission", new_callable=AsyncMock, return_value=_OK_RESULT) as mock_pub:
        await _fire_board(session, _fake_bot(), settings, _board_cfg(board), _FRESH_CUTOFF, _MT_MIDNIGHT)

    # Sub B cleaned up as duplicate (state=PUBLISHED, error mentions duplicate)
    attempt_b = await session.scalar(select(PublishAttempt).where(PublishAttempt.submission_id == sub_b.id))
    assert sub_b.state == SubmissionState.PUBLISHED.value
    assert attempt_b is not None and "duplicate" in (attempt_b.error or "")

    # Sub C published for real in the same tick
    attempt_c = await session.scalar(select(PublishAttempt).where(PublishAttempt.submission_id == sub_c.id))
    assert sub_c.state == SubmissionState.PUBLISHED.value
    assert attempt_c is not None and attempt_c.error is None and attempt_c.success is True
    mock_pub.assert_awaited_once()


async def test_duplicate_publish_queued_submission_returns_none(session, board):
    """publish_queued_submission returns None for duplicates so _fire_board can loop past them."""
    settings = _full_settings(board)

    sub_a = make_submission(board, state=SubmissionState.PUBLISHED.value, source_discord_message_id=10)
    session.add(sub_a)
    await session.flush()
    await _add_link(session, sub_a.id, "https://example.com/dup")
    session.add(PublishAttempt(submission_id=sub_a.id, success=True, error=None,
                               at_uri="at://did/old", bsky_url="https://bsky.app/old"))
    await session.flush()

    sub_b = make_submission(board, state=QUEUED, source_discord_message_id=11)
    session.add(sub_b)
    await session.flush()
    await _add_link(session, sub_b.id, "https://example.com/dup")

    result = await publish_queued_submission(session, settings, sub_b, MockDest())
    assert result is None, "duplicate cleanup must return None so the scheduler continues to next"


async def test_null_canonical_url_not_treated_as_duplicate(session, board):
    """Submissions with canonical_url=None must not match each other as duplicates.

    NULL == NULL uses IS NULL in SQL, which would incorrectly flag completely
    different submissions (whose canonicalization failed) as duplicates of each other.
    """
    settings = _full_settings(board)

    # Sub A: published, but canonical_url failed (None)
    sub_a = make_submission(board, state=SubmissionState.PUBLISHED.value, source_discord_message_id=20)
    session.add(sub_a)
    await session.flush()
    await _add_link(session, sub_a.id, "https://example.com/a", canonical_url=None)
    session.add(PublishAttempt(submission_id=sub_a.id, success=True, error=None,
                               at_uri="at://did/a", bsky_url="https://bsky.app/a"))
    await session.flush()

    # Sub B: QUEUED, different content but also has canonical_url=None
    sub_b = make_submission(board, state=QUEUED, source_discord_message_id=21)
    session.add(sub_b)
    await session.flush()
    await _add_link(session, sub_b.id, "https://example.com/b-completely-different", canonical_url=None)

    ok = PublishResult(success=True, at_uri="at://did/b", bsky_url="https://bsky.app/b")
    with patch("bot.publish.publish_submission", new_callable=AsyncMock, return_value=ok) as mock_pub:
        result = await publish_queued_submission(session, settings, sub_b, MockDest())

    assert result is True, "submission with null canonical_url must not be falsely suppressed"
    mock_pub.assert_awaited_once()


async def test_suppression_row_does_not_cascade_to_block_next_submission(session, board):
    """A duplicate-suppression row (success=True, error='duplicate:...') must not cause
    another submission with the same URL to be suppressed if no real publish exists.

    Only real publishes (success=True AND error IS NULL) should trigger dedup.
    Without this guard, a false-positive suppression on sub A would cascade to sub B.
    """
    settings = _full_settings(board)

    # Sub A: published state but its only attempt is a suppression row (error IS NOT NULL)
    sub_a = make_submission(board, state=SubmissionState.PUBLISHED.value, source_discord_message_id=30)
    session.add(sub_a)
    await session.flush()
    await _add_link(session, sub_a.id, "https://example.com/shared")
    session.add(PublishAttempt(
        submission_id=sub_a.id, success=True,
        error="duplicate: content already published by another submission",
        at_uri="at://did/ref", bsky_url="https://bsky.app/ref",
    ))
    await session.flush()

    # Sub B: QUEUED, same URL - the suppression row for A must NOT trigger dedup for B
    sub_b = make_submission(board, state=QUEUED, source_discord_message_id=31)
    session.add(sub_b)
    await session.flush()
    await _add_link(session, sub_b.id, "https://example.com/shared")

    ok = PublishResult(success=True, at_uri="at://did/b-real", bsky_url="https://bsky.app/b-real")
    with patch("bot.publish.publish_submission", new_callable=AsyncMock, return_value=ok) as mock_pub:
        result = await publish_queued_submission(session, settings, sub_b, MockDest())

    assert result is True, "suppression row must not cascade to suppress a later submission"
    mock_pub.assert_awaited_once()


# ---------------------------------------------------------------------------
# Dispatcher startup regression
# ---------------------------------------------------------------------------
#
# Bug: run_queue_dispatcher referenced settings.queue_fresh_daily_cap and
# settings.queue_backlog_daily_cap in its startup log. Neither attribute exists
# on Settings, causing a silent AttributeError that prevented the dispatcher
# from ever running after a bot restart.
#
# This test uses MagicMock(spec=Settings) so that any attribute access on a
# name that doesn't exist on the real Settings class raises AttributeError,
# exactly as it does in production. A pre-set stop event makes the coroutine
# exit immediately after the startup log so the test stays fast.


async def test_run_queue_dispatcher_startup_uses_only_real_settings_attributes():
    """Dispatcher startup must not reference attributes absent from Settings.

    Regression: queue_fresh_daily_cap / queue_backlog_daily_cap were referenced
    in the startup log but don't exist, crashing the task silently on every bot
    restart and stopping all queue activity.
    """
    stop = asyncio.Event()
    stop.set()  # exit immediately after the startup log

    settings = MagicMock(spec=Settings)
    settings.queue_timezone = "America/Denver"
    settings.queue_start_hour = 12
    settings.queue_min_daily = 1
    settings.queue_max_daily = 6
    settings.queue_target_days = 90
    settings.queue_fresh_window_hours = 72
    settings.boards = []

    # Must not raise AttributeError (or any other exception)
    await run_queue_dispatcher(bot=None, settings=settings, stop=stop)
