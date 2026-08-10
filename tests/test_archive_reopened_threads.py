"""Tests for the archive_reopened_threads one-shot (issue #65 curative cleanup).

Covers the two pieces that are testable without Discord: the idle predicate's
boundary, and the DB target selection (non-terminal submissions that still have a
thread; closed-state and threadless ones excluded).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bot.admin.archive_reopened_threads import _is_idle, find_targets
from bot.state import SubmissionState

from conftest import make_submission

_NOW = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)


# --- _is_idle boundary -------------------------------------------------------

def test_is_idle_true_when_older_than_cutoff():
    assert _is_idle(_NOW - timedelta(hours=25), _NOW, 24) is True


def test_is_idle_true_exactly_at_cutoff():
    assert _is_idle(_NOW - timedelta(hours=24), _NOW, 24) is True


def test_is_idle_false_when_recent():
    assert _is_idle(_NOW - timedelta(hours=23, minutes=59), _NOW, 24) is False


def test_is_idle_respects_custom_window():
    activity = _NOW - timedelta(hours=30)
    assert _is_idle(activity, _NOW, 24) is True
    assert _is_idle(activity, _NOW, 48) is False


# --- find_targets selection --------------------------------------------------

async def test_find_targets_picks_nonterminal_with_thread(session, board):
    sub = make_submission(
        board, state=SubmissionState.AWAITING_ALT_TEXT.value, thread_id=8001,
    )
    session.add(sub)
    await session.flush()

    assert await find_targets(session) == [(sub.id, 8001)]


async def test_find_targets_excludes_closed_states(session, board):
    for i, state in enumerate((
        SubmissionState.QUEUED.value,
        SubmissionState.PUBLISHED.value,
        SubmissionState.PUBLISH_FAILED.value,
    )):
        sub = make_submission(board, state=state, source_discord_message_id=100 + i, thread_id=9000 + i)
        session.add(sub)
    await session.flush()

    assert await find_targets(session) == []


async def test_find_targets_excludes_threadless(session, board):
    sub = make_submission(board, state=SubmissionState.AWAITING_SOURCE.value, thread_id=None)
    session.add(sub)
    await session.flush()

    assert await find_targets(session) == []


async def test_find_targets_includes_ready_to_queue(session, board):
    sub = make_submission(board, state=SubmissionState.READY_TO_QUEUE.value, thread_id=8002)
    session.add(sub)
    await session.flush()

    assert await find_targets(session) == [(sub.id, 8002)]
