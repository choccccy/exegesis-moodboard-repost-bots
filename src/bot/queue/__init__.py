"""Fresh/backlog queue: pick the next submission to publish per board.

Selection order within each board:
  1. Fresh submissions (created within the configured freshness window) — FIFO
  2. Backlog submissions (older than the freshness window) — FIFO

Both QUEUED and PUBLISH_FAILED submissions are candidates; PUBLISH_FAILED acts as
an automatic retry via the next available slot.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..models import PublishAttempt, Submission, SubmissionLink
from ..state import SubmissionState


async def pick_next_for_board(
    session: AsyncSession,
    board_id: int,
    fresh_cutoff_utc: datetime,
) -> Submission | None:
    """Return the next submission to publish for this board, or None if nothing is queued.

    Fresh submissions (created_at >= fresh_cutoff_utc) come before backlog, with FIFO
    ordering within each tier. Both QUEUED and PUBLISH_FAILED are included as candidates.
    """
    _eligible = (SubmissionState.QUEUED.value, SubmissionState.PUBLISH_FAILED.value)
    # NULL source_posted_at (pre-migration rows) sorts as backlog (priority=1).
    freshness_priority = case(
        (Submission.source_posted_at >= fresh_cutoff_utc, 0),
        else_=1,
    )
    return await session.scalar(
        select(Submission)
        .where(
            Submission.board_id == board_id,
            Submission.state.in_(_eligible),
        )
        .order_by(freshness_priority, Submission.source_posted_at.nulls_last(), Submission.created_at)
        .limit(1)
    )


async def count_posts_today(
    session: AsyncSession,
    board_id: int,
    since_utc: datetime,
) -> int:
    """Count successful publishes for this board on the current local day (since MT midnight)."""
    result = await session.scalar(
        select(func.count())
        .select_from(PublishAttempt)
        .join(Submission, PublishAttempt.submission_id == Submission.id)
        .where(
            Submission.board_id == board_id,
            PublishAttempt.success.is_(True),
            PublishAttempt.attempted_at >= since_utc,
        )
    )
    return result or 0


async def has_fresh_queued(
    session: AsyncSession,
    board_id: int,
    fresh_cutoff_utc: datetime,
) -> bool:
    """Return True if any QUEUED/PUBLISH_FAILED submission for this board is still fresh."""
    _eligible = (SubmissionState.QUEUED.value, SubmissionState.PUBLISH_FAILED.value)
    result = await session.scalar(
        select(func.count())
        .select_from(Submission)
        .where(
            Submission.board_id == board_id,
            Submission.state.in_(_eligible),
            Submission.source_posted_at >= fresh_cutoff_utc,
        )
    )
    return (result or 0) > 0


def daily_cap(fresh_available: bool, settings: Settings) -> int:
    """Return the daily post cap for this board given whether fresh content is available."""
    return settings.queue_fresh_daily_cap if fresh_available else settings.queue_backlog_daily_cap
