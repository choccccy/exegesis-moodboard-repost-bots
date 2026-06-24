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
