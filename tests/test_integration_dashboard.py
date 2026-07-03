"""Integration tests for dashboard queries.

Covers board_queue correctness including the naive/aware datetime mismatch
that caused the /boards/{board_name} page to return a 500 error.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from bot.dashboard.queries import QueuedItem, board_queue, board_stats, recent_publishes
from bot.models import PublishAttempt, Submission, SubmissionLink
from bot.state import SubmissionState

from conftest import make_submission


QUEUED = SubmissionState.QUEUED.value
FAILED = SubmissionState.PUBLISH_FAILED.value

_NOW_NAIVE = datetime.now()
_FRESH_NAIVE = _NOW_NAIVE - timedelta(hours=24)
_STALE_NAIVE = _NOW_NAIVE - timedelta(hours=96)


class _FakeSettings:
    queue_fresh_window_hours = 72

    def bluesky_handle_for(self, name):
        return f"{name}.bsky.social"


# ---------------------------------------------------------------------------
# board_queue - basic
# ---------------------------------------------------------------------------

async def test_unknown_board_returns_none(session, board):
    result_board, items = await board_queue(session, "nonexistent", _FakeSettings())
    assert result_board is None
    assert items == []


async def test_empty_queue(session, board):
    result_board, items = await board_queue(session, board.name, _FakeSettings())
    assert result_board is not None
    assert items == []


async def test_queued_submission_appears(session, board):
    sub = make_submission(board, state=QUEUED, source_discord_message_id=1, source_posted_at=_FRESH_NAIVE)
    session.add(sub)
    await session.flush()

    _, items = await board_queue(session, board.name, _FakeSettings())

    assert len(items) == 1
    assert items[0].submission_id == sub.id
    assert items[0].state == "queued"


async def test_failed_submission_appears_with_error(session, board):
    sub = make_submission(board, state=FAILED, source_discord_message_id=1)
    session.add(sub)
    await session.flush()
    attempt = PublishAttempt(submission_id=sub.id, success=False, error="atproto timeout")
    session.add(attempt)
    await session.flush()

    _, items = await board_queue(session, board.name, _FakeSettings())

    assert len(items) == 1
    assert items[0].state == "failed"
    assert items[0].error == "atproto timeout"


async def test_published_submission_not_in_queue(session, board):
    pub = make_submission(board, state=SubmissionState.PUBLISHED.value, source_discord_message_id=1)
    queued = make_submission(board, state=QUEUED, source_discord_message_id=2)
    session.add_all([pub, queued])
    await session.flush()

    _, items = await board_queue(session, board.name, _FakeSettings())

    assert len(items) == 1
    assert items[0].submission_id == queued.id


# ---------------------------------------------------------------------------
# board_queue - freshness classification
# ---------------------------------------------------------------------------

async def test_fresh_submission_marked_fresh(session, board):
    sub = make_submission(board, state=QUEUED, source_discord_message_id=1, source_posted_at=_FRESH_NAIVE)
    session.add(sub)
    await session.flush()

    _, items = await board_queue(session, board.name, _FakeSettings())

    assert items[0].is_fresh is True


async def test_stale_submission_not_fresh(session, board):
    sub = make_submission(board, state=QUEUED, source_discord_message_id=1, source_posted_at=_STALE_NAIVE)
    session.add(sub)
    await session.flush()

    _, items = await board_queue(session, board.name, _FakeSettings())

    assert items[0].is_fresh is False


async def test_null_source_posted_at_falls_back_to_created_at(session, board):
    # When source_posted_at is unknown (e.g. YouTube submissions), created_at
    # is used as the freshness date. A submission created just now is fresh.
    sub = make_submission(board, state=QUEUED, source_discord_message_id=1, source_posted_at=None)
    session.add(sub)
    await session.flush()

    _, items = await board_queue(session, board.name, _FakeSettings())

    assert items[0].is_fresh is True


async def test_naive_datetime_comparison_does_not_raise(session, board):
    # Regression: source_posted_at is stored as naive UTC in SQLite; comparing
    # against an aware fresh_cutoff previously raised TypeError in board_queue.
    sub = make_submission(board, state=QUEUED, source_discord_message_id=1, source_posted_at=_FRESH_NAIVE)
    session.add(sub)
    await session.flush()

    # Should not raise TypeError
    _, items = await board_queue(session, board.name, _FakeSettings())
    assert len(items) == 1


# ---------------------------------------------------------------------------
# board_queue - ordering (fresh before backlog)
# ---------------------------------------------------------------------------

async def test_fresh_ordered_before_stale(session, board):
    stale = make_submission(board, state=QUEUED, source_discord_message_id=1, source_posted_at=_STALE_NAIVE)
    fresh = make_submission(board, state=QUEUED, source_discord_message_id=2, source_posted_at=_FRESH_NAIVE)
    session.add_all([stale, fresh])
    await session.flush()

    _, items = await board_queue(session, board.name, _FakeSettings())

    assert items[0].submission_id == fresh.id
    assert items[1].submission_id == stale.id


# ---------------------------------------------------------------------------
# board_stats and recent_publishes - duplicate cleanup rows must be excluded
# ---------------------------------------------------------------------------
#
# Duplicate cleanup rows have success=True but error="duplicate: ...".
# They must NOT appear as real posts in the dashboard because:
# 1. They didn't actually publish new content.
# 2. Their attempted_at may have a timezone suffix that sorts above naive
#    timestamps in SQLite string comparison, hiding real recent posts.


class _BoardStatSettings:
    queue_timezone = "America/Denver"
    queue_fresh_window_hours = 72
    queue_target_days = 90
    queue_min_daily = 1
    queue_max_daily = 6
    boards = []

    def bluesky_handle_for(self, name):
        return f"{name}.exegesis.space"


async def test_board_stats_excludes_duplicate_cleanup_from_last_published(session, board):
    """board_stats last_published_at must reflect the most recent REAL post, not a cleanup row.

    Regression: before the fix, the last_attempt query used success.is_(True) without
    filtering error IS NULL, so duplicate cleanup rows appeared as the 'last post'.
    """
    real_sub = make_submission(board, state=SubmissionState.PUBLISHED.value, source_discord_message_id=1)
    dup_sub = make_submission(board, state=SubmissionState.PUBLISHED.value, source_discord_message_id=2)
    session.add_all([real_sub, dup_sub])
    await session.flush()

    # Real post - happened earlier
    real_attempt = PublishAttempt(
        submission_id=real_sub.id,
        success=True, error=None,
        bsky_url="https://bsky.app/profile/robots.exegesis.space/post/abc",
        at_uri="at://did:plc:test/app.bsky.feed.post/abc",
        attempted_at=datetime(2026, 7, 2, 12, 0, 0),
    )
    # Duplicate cleanup - happened later but must not be shown as a post
    dup_attempt = PublishAttempt(
        submission_id=dup_sub.id,
        success=True,
        error="duplicate: content already published by another submission",
        bsky_url="https://bsky.app/profile/robots.exegesis.space/post/abc",
        at_uri="at://did:plc:test/app.bsky.feed.post/abc",
        attempted_at=datetime(2026, 7, 2, 14, 0, 0),
    )
    session.add_all([real_attempt, dup_attempt])
    await session.flush()

    settings = _BoardStatSettings()
    settings.boards = []
    stats = await board_stats(session, settings)
    board_stat = next(s for s in stats if s.name == board.name)

    # last_published_at must be the REAL post, not the later duplicate cleanup
    assert board_stat.last_bsky_url == "https://bsky.app/profile/robots.exegesis.space/post/abc"
    assert board_stat.last_published_at == datetime(2026, 7, 2, 12, 0, 0)


async def test_recent_publishes_excludes_duplicate_cleanup_rows(session, board):
    """recent_publishes must not include rows where error IS NOT NULL.

    Regression: before the fix, duplicate cleanup rows appeared in the feed,
    making curators think content had been newly published when it had only
    been de-duplicated at publish time.
    """
    real_sub = make_submission(board, state=SubmissionState.PUBLISHED.value, source_discord_message_id=10)
    dup_sub = make_submission(board, state=SubmissionState.PUBLISHED.value, source_discord_message_id=11)
    session.add_all([real_sub, dup_sub])
    await session.flush()

    session.add(PublishAttempt(
        submission_id=real_sub.id,
        success=True, error=None,
        bsky_url="https://bsky.app/profile/robots.exegesis.space/post/real",
        at_uri="at://did:plc:test/app.bsky.feed.post/real",
        attempted_at=datetime(2026, 7, 2, 10, 0, 0),
    ))
    session.add(PublishAttempt(
        submission_id=dup_sub.id,
        success=True,
        error="duplicate: content already published by another submission",
        bsky_url="https://bsky.app/profile/robots.exegesis.space/post/real",
        at_uri="at://did:plc:test/app.bsky.feed.post/real",
        attempted_at=datetime(2026, 7, 2, 12, 0, 0),
    ))
    await session.flush()

    settings = _BoardStatSettings()
    publishes = await recent_publishes(session, settings, limit=10)

    assert len(publishes) == 1, "duplicate cleanup row must be excluded from recent_publishes"
    assert publishes[0].resolved_bsky_url == "https://bsky.app/profile/robots.exegesis.space/post/real"
