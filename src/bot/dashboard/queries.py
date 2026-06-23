"""Read-only DB queries for the dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Attachment, Board, PublishAttempt, Submission, SubmissionLink
from ..publish import at_uri_to_url
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
    post_type: str          # "repost" | "image" | "link"
    title: str | None
    author_display: str
    canonical_url: str | None
    resolved_bsky_url: str | None
    image_count: int
    source_posted_at: datetime | None
    source_posted_rel: str
    attempted_at: datetime
    attempted_rel: str


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

        if last_attempt:
            handle = settings.bluesky_handle_for(board.name)
            last_bsky_url = last_attempt.bsky_url or (
                at_uri_to_url(last_attempt.at_uri, handle) if last_attempt.at_uri else None
            )
        else:
            last_bsky_url = None

        stats.append(BoardStat(
            board_id=board.id,
            name=board.name,
            bluesky_handle=settings.bluesky_handle_for(board.name),
            queued_count=queued_count,
            failed_count=failed_count,
            today_count=today_count,
            cap=cap,
            fresh_mode=fresh_available,
            last_bsky_url=last_bsky_url,
            last_published_at=last_attempt.attempted_at if last_attempt else None,
            last_published_rel=_relative(last_attempt.attempted_at if last_attempt else None),
        ))

    return stats


async def recent_publishes(
    session: AsyncSession, settings: DashboardSettings, limit: int = 30
) -> list[RecentPublish]:
    image_count_sq = (
        select(func.count())
        .select_from(Attachment)
        .where(
            Attachment.submission_id == Submission.id,
            Attachment.is_image.is_(True),
        )
        .correlate(Submission)
        .scalar_subquery()
    )

    rows = await session.execute(
        select(
            PublishAttempt.bsky_url,
            PublishAttempt.at_uri,
            PublishAttempt.attempted_at,
            Board.name.label("board_name"),
            Submission.author_display,
            Submission.embed_title,
            Submission.source_posted_at,
            SubmissionLink.canonical_url,
            SubmissionLink.resolved_title,
            SubmissionLink.resolved_image_path,
            image_count_sq.label("image_count"),
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

    result = []
    for r in rows:
        handle = settings.bluesky_handle_for(r.board_name)
        resolved_bsky_url = r.bsky_url or (
            at_uri_to_url(r.at_uri, handle) if r.at_uri else None
        )
        if r.at_uri and "app.bsky.feed.repost" in r.at_uri:
            post_type = "repost"
        elif r.resolved_image_path:
            post_type = "image"
        else:
            post_type = "link"

        result.append(RecentPublish(
            board_name=r.board_name,
            post_type=post_type,
            title=r.embed_title or r.resolved_title,
            author_display=r.author_display or "",
            canonical_url=r.canonical_url,
            resolved_bsky_url=resolved_bsky_url,
            image_count=r.image_count or 0,
            source_posted_at=r.source_posted_at,
            source_posted_rel=_relative(r.source_posted_at),
            attempted_at=r.attempted_at,
            attempted_rel=_relative(r.attempted_at),
        ))
    return result


@dataclass
class QueuedItem:
    submission_id: int
    post_type: str
    title: str | None
    author_display: str
    canonical_url: str | None
    image_count: int
    is_fresh: bool
    source_posted_at: datetime | None
    source_posted_rel: str
    queued_since_rel: str
    state: str  # "queued" | "failed"
    error: str | None


async def board_queue(
    session: AsyncSession, board_name: str, settings: DashboardSettings
) -> tuple[Board | None, list[QueuedItem]]:
    board = await session.scalar(select(Board).where(Board.name == board_name))
    if board is None:
        return None, []

    now_utc = datetime.now(timezone.utc)
    fresh_cutoff = now_utc - timedelta(hours=settings.queue_fresh_window_hours)

    image_count_sq = (
        select(func.count())
        .select_from(Attachment)
        .where(Attachment.submission_id == Submission.id, Attachment.is_image.is_(True))
        .correlate(Submission)
        .scalar_subquery()
    )
    latest_error_sq = (
        select(PublishAttempt.error)
        .where(PublishAttempt.submission_id == Submission.id, PublishAttempt.success.is_(False))
        .order_by(PublishAttempt.attempted_at.desc())
        .limit(1)
        .correlate(Submission)
        .scalar_subquery()
    )

    _eligible = (SubmissionState.QUEUED.value, SubmissionState.PUBLISH_FAILED.value)

    rows = await session.execute(
        select(
            Submission.id,
            Submission.state,
            Submission.author_display,
            Submission.embed_title,
            Submission.source_posted_at,
            Submission.created_at,
            SubmissionLink.canonical_url,
            SubmissionLink.resolved_title,
            SubmissionLink.resolved_image_path,
            SubmissionLink.domain_family,
            image_count_sq.label("image_count"),
            latest_error_sq.label("error"),
        )
        .outerjoin(
            SubmissionLink,
            and_(
                SubmissionLink.submission_id == Submission.id,
                SubmissionLink.order_index == 0,
            ),
        )
        .where(Submission.board_id == board.id, Submission.state.in_(_eligible))
        .order_by(
            case(
                (
                    and_(
                        Submission.source_posted_at.isnot(None),
                        Submission.source_posted_at >= fresh_cutoff,
                    ),
                    0,
                ),
                else_=1,
            ),
            Submission.source_posted_at.nulls_last(),
            Submission.created_at,
        )
    )

    items: list[QueuedItem] = []
    for r in rows:
        is_fresh = r.source_posted_at is not None and r.source_posted_at >= fresh_cutoff
        if r.domain_family == "bluesky":
            post_type = "repost"
        elif r.resolved_image_path:
            post_type = "image"
        else:
            post_type = "link"
        items.append(QueuedItem(
            submission_id=r.id,
            post_type=post_type,
            title=r.embed_title or r.resolved_title,
            author_display=r.author_display or "",
            canonical_url=r.canonical_url,
            image_count=r.image_count or 0,
            is_fresh=is_fresh,
            source_posted_at=r.source_posted_at,
            source_posted_rel=_relative(r.source_posted_at),
            queued_since_rel=_relative(r.created_at),
            state="failed" if r.state == SubmissionState.PUBLISH_FAILED.value else "queued",
            error=r.error,
        ))
    return board, items
