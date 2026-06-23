"""Integration tests for queue scheduling functions.

Covers pick_next_for_board ordering and has_fresh_queued, including the
naive/aware datetime mismatch that caused SQLite comparison failures.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from bot.models import Submission
from bot.queue import has_fresh_queued, pick_next_for_board
from bot.state import SubmissionState

from conftest import make_submission


QUEUED = SubmissionState.QUEUED.value

# Naive UTC datetimes as SQLite stores them (no tzinfo).
_NOW_NAIVE = datetime(2026, 6, 23, 18, 0, 0)
_FRESH_NAIVE = _NOW_NAIVE - timedelta(hours=24)    # 24h ago — within 72h window
_STALE_NAIVE = _NOW_NAIVE - timedelta(hours=96)    # 96h ago — outside 72h window

# Aware cutoff as the scheduler passes (UTC-aware).
_CUTOFF_AWARE = (_NOW_NAIVE - timedelta(hours=72)).replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# pick_next_for_board — ordering
# ---------------------------------------------------------------------------

async def test_fresh_submission_picked_before_backlog(session, board):
    fresh = make_submission(board, state=QUEUED, source_discord_message_id=1, source_posted_at=_FRESH_NAIVE)
    stale = make_submission(board, state=QUEUED, source_discord_message_id=2, source_posted_at=_STALE_NAIVE)
    session.add_all([stale, fresh])  # add backlog first to rule out insertion order
    await session.flush()

    result = await pick_next_for_board(session, board.id, _CUTOFF_AWARE)

    assert result is not None
    assert result.id == fresh.id


async def test_null_source_posted_at_sorts_as_backlog(session, board):
    fresh = make_submission(board, state=QUEUED, source_discord_message_id=1, source_posted_at=_FRESH_NAIVE)
    no_time = make_submission(board, state=QUEUED, source_discord_message_id=2, source_posted_at=None)
    session.add_all([no_time, fresh])
    await session.flush()

    result = await pick_next_for_board(session, board.id, _CUTOFF_AWARE)

    assert result is not None
    assert result.id == fresh.id


async def test_returns_none_when_nothing_queued(session, board):
    result = await pick_next_for_board(session, board.id, _CUTOFF_AWARE)
    assert result is None


async def test_naive_datetime_comparison_does_not_raise(session, board):
    # Regression: aware cutoff passed to SQLite column storing naive datetimes
    # previously caused "can't compare offset-naive and offset-aware datetimes".
    sub = make_submission(board, state=QUEUED, source_discord_message_id=1, source_posted_at=_FRESH_NAIVE)
    session.add(sub)
    await session.flush()

    # Should not raise TypeError
    result = await pick_next_for_board(session, board.id, _CUTOFF_AWARE)
    assert result is not None


# ---------------------------------------------------------------------------
# has_fresh_queued
# ---------------------------------------------------------------------------

async def test_has_fresh_queued_true_when_fresh_exists(session, board):
    sub = make_submission(board, state=QUEUED, source_discord_message_id=1, source_posted_at=_FRESH_NAIVE)
    session.add(sub)
    await session.flush()

    assert await has_fresh_queued(session, board.id, _CUTOFF_AWARE) is True


async def test_has_fresh_queued_false_when_only_stale(session, board):
    sub = make_submission(board, state=QUEUED, source_discord_message_id=1, source_posted_at=_STALE_NAIVE)
    session.add(sub)
    await session.flush()

    assert await has_fresh_queued(session, board.id, _CUTOFF_AWARE) is False


async def test_has_fresh_queued_false_when_empty(session, board):
    assert await has_fresh_queued(session, board.id, _CUTOFF_AWARE) is False


async def test_has_fresh_queued_naive_cutoff_also_works(session, board):
    # Callers that strip tzinfo before passing should also work.
    cutoff_naive = _CUTOFF_AWARE.replace(tzinfo=None)
    sub = make_submission(board, state=QUEUED, source_discord_message_id=1, source_posted_at=_FRESH_NAIVE)
    session.add(sub)
    await session.flush()

    assert await has_fresh_queued(session, board.id, cutoff_naive) is True
