"""Tests for _queue_action - the state-transition guard in recompute_and_request.

These cover the invariants that were broken by two related bugs:
  1. evaluate_state is content-based and always returns READY_TO_QUEUE for a complete
     submission, so recompute_and_request must not blindly overwrite DB state - a QUEUED
     submission must not be downgraded back to ready_to_queue.
  2. A submission stuck at ready_to_queue from a previous run must still be transitioned
     to queued on re-evaluation (the "silent" path).
"""

import pytest

from bot.discord_ingest.service import _queue_action
from bot.state import SubmissionState


# Shorthand for state values
INTENT    = SubmissionState.INTENT_SUBMITTED.value
AWAITING  = SubmissionState.AWAITING_SOURCE.value
READY     = SubmissionState.READY_TO_QUEUE.value
QUEUED    = SubmissionState.QUEUED.value
PUBLISHED = SubmissionState.PUBLISHED.value
FAILED    = SubmissionState.PUBLISH_FAILED.value
RTQ       = SubmissionState.READY_TO_QUEUE  # enum member for evaluated state


# ---------------------------------------------------------------------------
# "none" - terminal states must never be downgraded
# ---------------------------------------------------------------------------

def test_queued_not_downgraded():
    assert _queue_action(QUEUED, RTQ) == "none"

def test_published_not_downgraded():
    assert _queue_action(PUBLISHED, RTQ) == "none"

def test_failed_not_downgraded():
    assert _queue_action(FAILED, RTQ) == "none"

def test_non_ready_evaluated_state_is_none():
    # evaluate_state returned something other than READY_TO_QUEUE - no queuing action
    assert _queue_action(INTENT, SubmissionState.AWAITING_SOURCE) == "none"
    assert _queue_action(READY, SubmissionState.AWAITING_ALT_TEXT) == "none"


# ---------------------------------------------------------------------------
# "silent" - stuck at ready_to_queue from a prior run
# ---------------------------------------------------------------------------

def test_stuck_ready_to_queue_is_silent():
    # Submission was already ready_to_queue (confirmation/preview already sent).
    # Re-evaluation must transition it to queued without re-posting messages.
    assert _queue_action(READY, RTQ) == "silent"


# ---------------------------------------------------------------------------
# "fresh" - first-time transition from an earlier state
# ---------------------------------------------------------------------------

def test_intent_submitted_to_ready_is_fresh():
    assert _queue_action(INTENT, RTQ) == "fresh"

def test_awaiting_source_to_ready_is_fresh():
    assert _queue_action(AWAITING, RTQ) == "fresh"

def test_awaiting_alt_text_to_ready_is_fresh():
    assert _queue_action(SubmissionState.AWAITING_ALT_TEXT.value, RTQ) == "fresh"

def test_awaiting_image_to_ready_is_fresh():
    assert _queue_action(SubmissionState.AWAITING_IMAGE.value, RTQ) == "fresh"


# ---------------------------------------------------------------------------
# recompute_and_request - integration: request creation per gap
# (real in-memory DB via the shared session fixture, MockDest as the thread)
# ---------------------------------------------------------------------------

from sqlalchemy import select

from bot.discord_ingest.service import recompute_and_request
from bot.models import (
    Attachment,
    AttachmentAltTextRequest,
    CancellationRequest,
    ConfirmationRequest,
    ContentLabelRequest,
    ImageRequest,
    MetadataRequest,
    SourceRequest,
    Submission,
    SubmissionLink,
    SupplementalImageRequest,
)
from bot.state import AltTextStatus

from conftest import MockDest, make_submission, make_test_settings


async def _count(session, model, submission_id: int) -> int:
    rows = list(await session.scalars(select(model).where(model.submission_id == submission_id)))
    return len(rows)


def _add_link(session, sub, **kw) -> SubmissionLink:
    defaults = dict(
        submission_id=sub.id,
        order_index=0,
        raw_url="https://example.com/post",
        canonical_url="https://example.com/post",
        domain_family="other",
    )
    defaults.update(kw)
    link = SubmissionLink(**defaults)
    session.add(link)
    return link


async def test_recompute_no_link_posts_source_request(session, board):
    """No links at all: state -> awaiting_source, SourceRequest + cancel posted."""
    sub = make_submission(board)
    session.add(sub)
    await session.flush()

    dest = MockDest()
    state = await recompute_and_request(session, sub, settings=make_test_settings(), destination=dest)

    assert state == SubmissionState.AWAITING_SOURCE
    assert sub.state == AWAITING
    assert await _count(session, SourceRequest, sub.id) == 1
    assert await _count(session, CancellationRequest, sub.id) == 1


async def test_recompute_unresolved_link_posts_metadata_request(session, board):
    """External link with no resolved metadata: awaiting_better_link, MetadataRequest
    posted, and the IMAGE request suppressed while METADATA is open.
    """
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    _add_link(session, sub, resolved_via=None)
    await session.flush()

    dest = MockDest()
    state = await recompute_and_request(session, sub, settings=make_test_settings(), destination=dest)

    assert state == SubmissionState.AWAITING_BETTER_LINK
    assert await _count(session, MetadataRequest, sub.id) == 1
    assert await _count(session, ImageRequest, sub.id) == 0  # suppressed by open METADATA


async def test_recompute_resolved_link_without_image_posts_image_request(session, board):
    """Metadata resolved but no thumbnail downloaded: awaiting_image, ImageRequest posted."""
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    _add_link(session, sub, resolved_via="opengraph", resolved_title="Post", resolved_image_path=None)
    await session.flush()

    dest = MockDest()
    state = await recompute_and_request(session, sub, settings=make_test_settings(), destination=dest)

    assert state == SubmissionState.AWAITING_IMAGE
    assert await _count(session, ImageRequest, sub.id) == 1


async def test_recompute_needed_alt_text_posts_request(session, board):
    """Image attachment with alt text NEEDED: awaiting_alt_text, per-attachment request."""
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    _add_link(session, sub, resolved_via="opengraph", resolved_title="Post", resolved_image_path="/vol/t.jpg")
    att = Attachment(
        submission_id=sub.id,
        discord_attachment_id=1,
        filename="robot.jpg",
        discord_url="https://cdn.discord.com/robot.jpg",
        is_image=True,
        alt_text_status=AltTextStatus.NEEDED.value,
    )
    session.add(att)
    await session.flush()

    dest = MockDest()
    state = await recompute_and_request(session, sub, settings=make_test_settings(), destination=dest)

    assert state == SubmissionState.AWAITING_ALT_TEXT
    reqs = list(await session.scalars(
        select(AttachmentAltTextRequest).where(AttachmentAltTextRequest.submission_id == sub.id)
    ))
    assert len(reqs) == 1
    assert reqs[0].attachment_id == att.id
    # no local copy of the image, so the prompt falls back to the Discord URL
    assert any("cdn.discord.com/robot.jpg" in s for s in dest.sent)


async def test_recompute_graphic_classification_posts_request(session, board):
    """Boards that require graphic classification get a ContentLabelRequest once."""
    sub = make_submission(board, graphic_classification_required=True)
    session.add(sub)
    await session.flush()

    dest = MockDest()
    await recompute_and_request(session, sub, settings=make_test_settings(), destination=dest)
    assert await _count(session, ContentLabelRequest, sub.id) == 1

    # posted once only - the notice is not repeated on the next recompute
    await recompute_and_request(session, sub, settings=make_test_settings(), destination=dest)
    assert await _count(session, ContentLabelRequest, sub.id) == 1


async def test_recompute_ready_to_queue_posts_preview_and_confirmation(session, board):
    """Complete submission reaching READY_TO_QUEUE for the first time: preview
    pages + confirmation prompt posted, ConfirmationRequest recorded.
    """
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    _add_link(
        session, sub,
        resolved_via="opengraph",
        resolved_title="Cool Robot",
        resolved_image_path="/vol/thumb.jpg",
    )
    await session.flush()

    dest = MockDest()
    state = await recompute_and_request(session, sub, settings=make_test_settings(), destination=dest)

    assert state == SubmissionState.READY_TO_QUEUE
    assert sub.state == READY
    assert await _count(session, ConfirmationRequest, sub.id) == 1
    assert any("Cool Robot" in s for s in dest.sent)  # preview includes the title


async def test_recompute_reentrant_does_not_duplicate_open_requests(session, board):
    """A second recompute while requests are still open must not re-post them."""
    sub = make_submission(board)
    session.add(sub)
    await session.flush()

    dest = MockDest()
    settings = make_test_settings()
    await recompute_and_request(session, sub, settings=settings, destination=dest)
    await recompute_and_request(session, sub, settings=settings, destination=dest)

    assert await _count(session, SourceRequest, sub.id) == 1
    assert await _count(session, CancellationRequest, sub.id) == 1
    assert await _count(session, SupplementalImageRequest, sub.id) == 1


async def test_recompute_from_reply_on_queued_confirms_and_archives(session, board):
    """A reply-driven recompute on an already-queued submission with no remaining
    alt-text gaps posts the updated notice and archives the thread; the QUEUED
    state is never downgraded.
    """
    sub = make_submission(board, state=QUEUED)
    session.add(sub)
    await session.flush()
    _add_link(
        session, sub,
        resolved_via="opengraph",
        resolved_title="Cool Robot",
        resolved_image_path="/vol/thumb.jpg",
    )
    await session.flush()

    dest = MockDest()
    await recompute_and_request(
        session, sub, settings=make_test_settings(), destination=dest, from_reply=True
    )

    assert sub.state == QUEUED  # not downgraded to ready_to_queue
    assert any(s.startswith("[archive]") for s in dest.sent)
    # no confirmation re-posted for a terminal-state submission
    assert await _count(session, ConfirmationRequest, sub.id) == 0
