"""Submission state machine and the readiness evaluator.

This module is deliberately free of any DB or Discord imports so it can be unit
tested in isolation. The evaluator consumes plain snapshots of a submission's
links / attachments / moderation status and reports which information gaps remain.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class SubmissionState(str, enum.Enum):
    INTENT_SUBMITTED = "intent_submitted"
    AWAITING_SOURCE = "awaiting_source"
    AWAITING_ALT_TEXT = "awaiting_alt_text"
    AWAITING_GRAPHIC_CLASSIFICATION = "awaiting_graphic_classification"
    READY_TO_QUEUE = "ready_to_queue"
    # Present for forward-compatibility; unreachable in Milestone 1.
    QUEUED = "queued"
    PUBLISHED = "published"
    PUBLISH_FAILED = "publish_failed"


class AltTextStatus(str, enum.Enum):
    NEEDED = "needed"
    PROVIDED = "provided"
    NOT_REQUIRED = "not_required"  # e.g. non-image attachment


class GraphicStatus(str, enum.Enum):
    UNKNOWN = "unknown"
    GRAPHIC = "graphic"
    NOT_GRAPHIC = "not_graphic"


class Gap(str, enum.Enum):
    """A category of missing information that blocks publication."""

    SOURCE = "source"
    ALT_TEXT = "alt_text"
    GRAPHIC = "graphic"


@dataclass(frozen=True)
class SubmissionSnapshot:
    """Minimal view of a submission used to compute readiness."""

    has_canonical_link: bool
    # alt_text_status per image attachment (non-image attachments excluded)
    image_alt_statuses: list[AltTextStatus]
    graphic_status: GraphicStatus
    # Whether this board mandates an explicit graphic yes/no answer. In M1 we
    # only require it when an operator has flagged the need; NSFW labeling is
    # board-level metadata and does not by itself block on graphic classification.
    graphic_classification_required: bool


def missing_gaps(snap: SubmissionSnapshot) -> list[Gap]:
    """Return the ordered list of information gaps still blocking the submission."""
    gaps: list[Gap] = []
    if not snap.has_canonical_link:
        gaps.append(Gap.SOURCE)
    if any(s == AltTextStatus.NEEDED for s in snap.image_alt_statuses):
        gaps.append(Gap.ALT_TEXT)
    if snap.graphic_classification_required and snap.graphic_status == GraphicStatus.UNKNOWN:
        gaps.append(Gap.GRAPHIC)
    return gaps


def evaluate_state(snap: SubmissionSnapshot) -> SubmissionState:
    """Map the current information gaps onto a submission state.

    Precedence when multiple gaps exist: source > alt text > graphic. The first
    unmet requirement names the state; when nothing is missing the submission is
    ready to queue.
    """
    gaps = missing_gaps(snap)
    if not gaps:
        return SubmissionState.READY_TO_QUEUE
    first = gaps[0]
    if first == Gap.SOURCE:
        return SubmissionState.AWAITING_SOURCE
    if first == Gap.ALT_TEXT:
        return SubmissionState.AWAITING_ALT_TEXT
    return SubmissionState.AWAITING_GRAPHIC_CLASSIFICATION
