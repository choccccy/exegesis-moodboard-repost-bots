"""Integration tests for recompute_and_request state transitions.

These tests exercise the function against a real in-memory SQLite database and a
mock Discord destination.  The bugs that motivated them:

  1. QUEUED/PUBLISHED/PUBLISH_FAILED submissions were downgraded back to ready_to_queue
     because evaluate_state is content-based and always returns READY_TO_QUEUE for a
     complete submission - we must not blindly overwrite the persisted state.

  2. Submissions stuck at ready_to_queue from a prior run could not advance to queued
     because READY_TO_QUEUE was in the _already_done guard set.

  3. Request rows (SourceRequest, etc.) must be idempotent - calling recompute twice
     must not post duplicate requests to Discord or insert duplicate DB rows.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select

from bot.discord_ingest import replies
from bot.discord_ingest.service import recompute_and_request
from bot.models import (
    Attachment,
    Board,
    MetadataRequest,
    Submission,
    SubmissionLink,
    SourceRequest,
)
from bot.state import AltTextStatus, SubmissionState

from conftest import MockDest, make_submission


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

async def _add_link(session, submission: Submission, *, canonical_url: str = "https://example.com/post/1", resolved_via: str = "opengraph") -> SubmissionLink:
    link = SubmissionLink(
        submission_id=submission.id,
        order_index=0,
        raw_url=canonical_url,
        canonical_url=canonical_url,
        domain_family="web",
        resolved_via=resolved_via,
        resolved_title="A Title",
        resolved_description="A description",
        resolved_image_path="/data/thumb.jpg",
    )
    session.add(link)
    await session.flush()
    return link


async def _recompute(session, submission, board):
    dest = MockDest()
    await recompute_and_request(session, submission, settings=None, destination=dest)
    await session.flush()
    return dest


# ---------------------------------------------------------------------------
# Fresh first-time transition: INTENT_SUBMITTED → QUEUED
# ---------------------------------------------------------------------------

async def test_fresh_transition_reaches_queued(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value)
    session.add(sub)
    await session.flush()
    await _add_link(session, sub)

    dest = await _recompute(session, sub, board)

    assert sub.state == SubmissionState.QUEUED.value
    assert replies.ready_confirmation() in dest.sent
    assert replies.queued_notice() in dest.sent


# ---------------------------------------------------------------------------
# Stuck READY_TO_QUEUE → QUEUED silently (no duplicate confirmation messages)
# ---------------------------------------------------------------------------

async def test_stuck_ready_to_queue_transitions_silently(session, board):
    sub = make_submission(board, state=SubmissionState.READY_TO_QUEUE.value)
    session.add(sub)
    await session.flush()
    await _add_link(session, sub)

    dest = await _recompute(session, sub, board)

    assert sub.state == SubmissionState.QUEUED.value
    # The confirmation was already sent in a prior run - must not be re-sent.
    assert replies.ready_confirmation() not in dest.sent
    assert replies.queued_notice() not in dest.sent


# ---------------------------------------------------------------------------
# No state downgrade for terminal states
# ---------------------------------------------------------------------------

async def test_queued_not_downgraded(session, board):
    sub = make_submission(board, state=SubmissionState.QUEUED.value)
    session.add(sub)
    await session.flush()
    await _add_link(session, sub)

    await _recompute(session, sub, board)

    assert sub.state == SubmissionState.QUEUED.value


async def test_published_not_downgraded(session, board):
    sub = make_submission(board, state=SubmissionState.PUBLISHED.value)
    session.add(sub)
    await session.flush()
    await _add_link(session, sub)

    await _recompute(session, sub, board)

    assert sub.state == SubmissionState.PUBLISHED.value


async def test_publish_failed_not_downgraded(session, board):
    sub = make_submission(board, state=SubmissionState.PUBLISH_FAILED.value)
    session.add(sub)
    await session.flush()
    await _add_link(session, sub)

    await _recompute(session, sub, board)

    assert sub.state == SubmissionState.PUBLISH_FAILED.value


# ---------------------------------------------------------------------------
# Missing source: posts a source request, sets AWAITING_SOURCE
# ---------------------------------------------------------------------------

async def test_missing_source_posts_request(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value)
    session.add(sub)
    await session.flush()
    # No link added → SOURCE gap

    dest = await _recompute(session, sub, board)

    assert sub.state == SubmissionState.AWAITING_SOURCE.value
    assert any("source URL" in m for m in dest.sent)

    # One SourceRequest row inserted
    count = await session.scalar(
        select(SourceRequest).where(SourceRequest.submission_id == sub.id)
    )
    assert count is not None


# ---------------------------------------------------------------------------
# Idempotency: calling recompute twice does not duplicate requests
# ---------------------------------------------------------------------------

async def test_source_request_not_duplicated(session, board):
    sub = make_submission(board, state=SubmissionState.AWAITING_SOURCE.value)
    session.add(sub)
    await session.flush()

    dest1 = await _recompute(session, sub, board)
    dest2 = await _recompute(session, sub, board)

    source_msg = replies.source_request(f"<@{sub.author_id}>")
    assert dest1.sent.count(source_msg) == 1
    assert dest2.sent.count(source_msg) == 0  # already open, not re-posted

    rows = list(await session.scalars(
        select(SourceRequest).where(SourceRequest.submission_id == sub.id)
    ))
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# IMAGE gap suppressed while METADATA gap is open
# ---------------------------------------------------------------------------

async def test_image_request_suppressed_while_metadata_open(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value)
    session.add(sub)
    await session.flush()

    # External link with no image and no resolved metadata → IMAGE + METADATA gaps
    link = SubmissionLink(
        submission_id=sub.id,
        order_index=0,
        raw_url="https://example.com/post/1",
        canonical_url="https://example.com/post/1",
        domain_family="web",
        resolved_via="none",  # triggers METADATA gap
        resolved_image_path=None,
    )
    session.add(link)
    await session.flush()

    dest = await _recompute(session, sub, board)

    image_req = replies.image_request(f"<@{sub.author_id}>")
    assert any("embeddable" in m for m in dest.sent), "expected metadata request"
    assert image_req not in dest.sent, "image request must be suppressed while metadata is open"
