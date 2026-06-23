"""Integration tests for dashboard queries.

Covers board_queue correctness including the naive/aware datetime mismatch
that caused the /boards/{board_name} page to return a 500 error.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from bot.dashboard.queries import QueuedItem, board_queue
from bot.models import PublishAttempt, Submission, SubmissionLink
from bot.state import SubmissionState

from conftest import make_submission


QUEUED = SubmissionState.QUEUED.value
FAILED = SubmissionState.PUBLISH_FAILED.value

_NOW_NAIVE = datetime(2026, 6, 23, 18, 0, 0)
_FRESH_NAIVE = _NOW_NAIVE - timedelta(hours=24)
_STALE_NAIVE = _NOW_NAIVE - timedelta(hours=96)


class _FakeSettings:
    queue_fresh_window_hours = 72

    def bluesky_handle_for(self, name):
        return f"{name}.bsky.social"


# ---------------------------------------------------------------------------
# board_queue — basic
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
# board_queue — freshness classification
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
# board_queue — ordering (fresh before backlog)
# ---------------------------------------------------------------------------

async def test_fresh_ordered_before_stale(session, board):
    stale = make_submission(board, state=QUEUED, source_discord_message_id=1, source_posted_at=_STALE_NAIVE)
    fresh = make_submission(board, state=QUEUED, source_discord_message_id=2, source_posted_at=_FRESH_NAIVE)
    session.add_all([stale, fresh])
    await session.flush()

    _, items = await board_queue(session, board.name, _FakeSettings())

    assert items[0].submission_id == fresh.id
    assert items[1].submission_id == stale.id
