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

from unittest.mock import MagicMock

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
    SupplementalImageRequest,
)
from bot.state import AltTextStatus, SubmissionState

from conftest import MockDest, make_submission


def _mock_settings():
    s = MagicMock()
    s.board_for_channel.return_value = None
    s.dashboard_url = None
    return s


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


async def _recompute(session, submission, board, settings=None):
    dest = MockDest()
    await recompute_and_request(session, submission, settings=settings or _mock_settings(), destination=dest)
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
    assert any("queued" in m.lower() for m in dest.sent)


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
    assert not any("queued" in m.lower() for m in dest.sent)


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

    source_msg = replies.source_request()
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

    assert any("embeddable" in m for m in dest.sent), "expected metadata request"
    assert replies.image_request() not in dest.sent, "image request must be suppressed while metadata is open"


# ---------------------------------------------------------------------------
# SupplementalImageRequest: posted for active submissions, not for terminal ones
# ---------------------------------------------------------------------------

async def test_supplemental_image_request_posted_for_active_submission(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value)
    session.add(sub)
    await session.flush()

    dest = await _recompute(session, sub, board)

    assert replies.supplemental_image_request() in dest.sent

    # DB row created
    row = await session.scalar(
        select(SupplementalImageRequest).where(SupplementalImageRequest.submission_id == sub.id)
    )
    assert row is not None


async def test_supplemental_image_request_not_duplicated(session, board):
    sub = make_submission(board, state=SubmissionState.AWAITING_SOURCE.value)
    session.add(sub)
    await session.flush()

    dest1 = await _recompute(session, sub, board)
    dest2 = await _recompute(session, sub, board)

    supp_msg = replies.supplemental_image_request()
    assert dest1.sent.count(supp_msg) == 1
    assert dest2.sent.count(supp_msg) == 0  # open request already exists, not re-posted

    rows = list(await session.scalars(
        select(SupplementalImageRequest).where(SupplementalImageRequest.submission_id == sub.id)
    ))
    assert len(rows) == 1


async def test_supplemental_image_request_suppressed_for_queued(session, board):
    sub = make_submission(board, state=SubmissionState.QUEUED.value)
    session.add(sub)
    await session.flush()
    await _add_link(session, sub)
    dest = await _recompute(session, sub, board)
    assert replies.supplemental_image_request() not in dest.sent


async def test_supplemental_image_request_suppressed_for_published(session, board):
    sub = make_submission(board, state=SubmissionState.PUBLISHED.value)
    session.add(sub)
    await session.flush()
    await _add_link(session, sub)
    dest = await _recompute(session, sub, board)
    assert replies.supplemental_image_request() not in dest.sent


async def test_supplemental_image_request_suppressed_for_publish_failed(session, board):
    sub = make_submission(board, state=SubmissionState.PUBLISH_FAILED.value)
    session.add(sub)
    await session.flush()
    await _add_link(session, sub)
    dest = await _recompute(session, sub, board)
    assert replies.supplemental_image_request() not in dest.sent


# ---------------------------------------------------------------------------
# Cancel request: posted once, never duplicated
# ---------------------------------------------------------------------------

async def test_cancel_request_not_duplicated(session, board):
    sub = make_submission(board, state=SubmissionState.AWAITING_SOURCE.value)
    session.add(sub)
    await session.flush()

    dest1 = await _recompute(session, sub, board)
    dest2 = await _recompute(session, sub, board)

    cancel_msg = replies.cancel_request()
    assert dest1.sent.count(cancel_msg) == 1
    assert dest2.sent.count(cancel_msg) == 0


# ---------------------------------------------------------------------------
# QUEUED submissions never downgrade even if data looks incomplete
# ---------------------------------------------------------------------------

async def test_queued_not_downgraded_even_without_link(session, board):
    # Simulate a QUEUED submission where the SubmissionLink was somehow removed.
    # Evaluate_state would see SOURCE gap and want to set AWAITING_SOURCE,
    # but the terminal-state guard must prevent any downgrade.
    sub = make_submission(board, state=SubmissionState.QUEUED.value)
    session.add(sub)
    await session.flush()
    # Intentionally NOT adding a link — evaluate_state sees a SOURCE gap.

    await _recompute(session, sub, board)

    assert sub.state == SubmissionState.QUEUED.value


# ---------------------------------------------------------------------------
# ALT_TEXT gap: bot posts request per image needing alt text
# ---------------------------------------------------------------------------

async def test_alt_text_request_posted_per_image(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value)
    session.add(sub)
    await session.flush()
    await _add_link(session, sub)

    # Add two images needing alt text
    from bot.models import Attachment
    from bot.state import AltTextStatus
    for i in range(2):
        session.add(Attachment(
            submission_id=sub.id,
            discord_attachment_id=10000 + i,
            filename=f"robot{i}.jpg",
            discord_url=f"https://cdn.discord.com/robot{i}.jpg",
            is_image=True,
            alt_text_status=AltTextStatus.NEEDED.value,
        ))
    await session.flush()

    dest = await _recompute(session, sub, board)

    alt_text_msgs = [m for m in dest.sent if "alt text" in m.lower()]
    assert len(alt_text_msgs) == 2


async def test_alt_text_request_not_duplicated(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value)
    session.add(sub)
    await session.flush()
    await _add_link(session, sub)

    from bot.models import Attachment
    from bot.state import AltTextStatus
    session.add(Attachment(
        submission_id=sub.id,
        discord_attachment_id=20000,
        filename="robot.jpg",
        discord_url="https://cdn.discord.com/robot.jpg",
        is_image=True,
        alt_text_status=AltTextStatus.NEEDED.value,
    ))
    await session.flush()

    dest1 = await _recompute(session, sub, board)
    dest2 = await _recompute(session, sub, board)

    alt_text_msgs1 = [m for m in dest1.sent if "alt text" in m.lower()]
    alt_text_msgs2 = [m for m in dest2.sent if "alt text" in m.lower()]
    assert len(alt_text_msgs1) == 1
    assert len(alt_text_msgs2) == 0  # already open, not re-posted
