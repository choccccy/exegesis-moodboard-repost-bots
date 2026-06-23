"""Read-only DB queries for the dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Board, PublishAttempt, Submission, SubmissionLink
from ..queue import count_posts_today, daily_cap, has_fresh_queued
from ..state import SubmissionState
from .settings import DashboardSettings


def _mt_midnight(now_utc: datetime, tz: ZoneInfo) -> datetime:
    midnight_local = now_utc.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight_local.astimezone(timezone.utc)


def _relative(dt: datetime | None) -> str:
    if dt is None:
        return "never"
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = now - dt
    s = int(delta.total_seconds())
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


@dataclass
class BoardStat:
    board_id: int
    name: str
    bluesky_handle: str | None
    queued_count: int
    failed_count: int
    today_count: int
    cap: int
    fresh_mode: bool
    last_bsky_url: str | None
    last_published_at: datetime | None
    last_published_rel: str


@dataclass
class RecentPublish:
    board_name: str
    canonical_url: str | None
    bsky_url: str | None
    at_uri: str | None
    attempted_at: datetime
    attempted_rel: str


@dataclass
class FailedSub:
    submission_id: int
    board_name: str
    canonical_url: str | None
    error: str | None
    failed_at: datetime | None
    failed_rel: str


async def board_stats(session: AsyncSession, settings: DashboardSettings) -> list[BoardStat]:
    tz = ZoneInfo(settings.queue_timezone)
    now_utc = datetime.now(timezone.utc)
    fresh_cutoff = now_utc - timedelta(hours=settings.queue_fresh_window_hours)
    mt_midnight = _mt_midnight(now_utc, tz)

    boards = list(await session.scalars(select(Board).order_by(Board.name)))
    stats: list[BoardStat] = []

    for board in boards:
        _eligible = (SubmissionState.QUEUED.value, SubmissionState.PUBLISH_FAILED.value)

        queued_count = await session.scalar(
            select(func.count()).select_from(Submission).where(
                Submission.board_id == board.id,
                Submission.state.in_(_eligible),
            )
        ) or 0

        failed_count = await session.scalar(
            select(func.count()).select_from(Submission).where(
                Submission.board_id == board.id,
                Submission.state == SubmissionState.PUBLISH_FAILED.value,
            )
        ) or 0

        today_count = await count_posts_today(session, board.id, mt_midnight)
        fresh_available = await has_fresh_queued(session, board.id, fresh_cutoff)
        cap = daily_cap(fresh_available, settings)

        last_attempt = await session.scalar(
            select(PublishAttempt)
            .join(Submission, PublishAttempt.submission_id == Submission.id)
            .where(
                Submission.board_id == board.id,
                PublishAttempt.success.is_(True),
            )
            .order_by(PublishAttempt.attempted_at.desc())
            .limit(1)
        )

        stats.append(BoardStat(
            board_id=board.id,
            name=board.name,
            bluesky_handle=settings.bluesky_handle_for(board.name),
            queued_count=queued_count,
            failed_count=failed_count,
            today_count=today_count,
            cap=cap,
            fresh_mode=fresh_available,
            last_bsky_url=last_attempt.bsky_url if last_attempt else None,
            last_published_at=last_attempt.attempted_at if last_attempt else None,
            last_published_rel=_relative(last_attempt.attempted_at if last_attempt else None),
        ))

    return stats


async def recent_publishes(session: AsyncSession, limit: int = 30) -> list[RecentPublish]:
    rows = await session.execute(
        select(
            PublishAttempt.bsky_url,
            PublishAttempt.at_uri,
            PublishAttempt.attempted_at,
            Board.name.label("board_name"),
            SubmissionLink.canonical_url,
        )
        .join(Submission, PublishAttempt.submission_id == Submission.id)
        .join(Board, Board.id == Submission.board_id)
        .outerjoin(
            SubmissionLink,
            and_(
                SubmissionLink.submission_id == Submission.id,
                SubmissionLink.order_index == 0,
            ),
        )
        .where(PublishAttempt.success.is_(True))
        .order_by(PublishAttempt.attempted_at.desc())
        .limit(limit)
    )
    return [
        RecentPublish(
            board_name=r.board_name,
            canonical_url=r.canonical_url,
            bsky_url=r.bsky_url,
            at_uri=r.at_uri,
            attempted_at=r.attempted_at,
            attempted_rel=_relative(r.attempted_at),
        )
        for r in rows
    ]


async def failed_submissions(session: AsyncSession) -> list[FailedSub]:
    rows = await session.execute(
        select(
            Submission.id,
            Board.name.label("board_name"),
            SubmissionLink.canonical_url,
        )
        .join(Board, Board.id == Submission.board_id)
        .outerjoin(
            SubmissionLink,
            and_(
                SubmissionLink.submission_id == Submission.id,
                SubmissionLink.order_index == 0,
            ),
        )
        .where(Submission.state == SubmissionState.PUBLISH_FAILED.value)
        .order_by(Submission.id.desc())
    )
    result: list[FailedSub] = []
    for r in rows:
        last_attempt = await session.scalar(
            select(PublishAttempt)
            .where(
                PublishAttempt.submission_id == r.id,
                PublishAttempt.success.is_(False),
            )
            .order_by(PublishAttempt.attempted_at.desc())
            .limit(1)
        )
        result.append(FailedSub(
            submission_id=r.id,
            board_name=r.board_name,
            canonical_url=r.canonical_url,
            error=last_attempt.error if last_attempt else None,
            failed_at=last_attempt.attempted_at if last_attempt else None,
            failed_rel=_relative(last_attempt.attempted_at if last_attempt else None),
        ))
    return result
