"""Read-only DB queries for the dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import (
    Attachment,
    AttachmentAltTextRequest,
    Board,
    BotError,
    ContentLabelRequest,
    ImageRequest,
    MetadataRequest,
    PublishAttempt,
    SourceRequest,
    Submission,
    SubmissionLink,
    SubmissionThread,
    YoutubePlaylistAdd,
)
from ..publish import at_uri_to_url
from ..queue import count_posts_today, count_queued_for_board, daily_cap, has_fresh_queued
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
    youtube_playlist_id: str | None = None
    youtube_playlist_add_count: int = 0


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
        _eligible = (
            SubmissionState.QUEUED.value,
            SubmissionState.PUBLISH_FAILED.value,
            SubmissionState.READY_TO_QUEUE.value,
        )

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
        queue_size_for_cap = await count_queued_for_board(session, board.id)
        cap = daily_cap(queue_size_for_cap, settings)

        last_attempt = await session.scalar(
            select(PublishAttempt)
            .join(Submission, PublishAttempt.submission_id == Submission.id)
            .where(
                Submission.board_id == board.id,
                PublishAttempt.success.is_(True),
                PublishAttempt.error.is_(None),
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

        board_cfg = next((b for b in settings.boards if b.name == board.name), None)
        playlist_id = board_cfg.youtube_playlist_id if board_cfg else None
        playlist_add_count = 0
        if playlist_id:
            playlist_add_count = await session.scalar(
                select(func.count()).select_from(YoutubePlaylistAdd).where(
                    YoutubePlaylistAdd.board_id == board.id,
                    YoutubePlaylistAdd.success.is_(True),
                )
            ) or 0

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
            youtube_playlist_id=playlist_id,
            youtube_playlist_add_count=playlist_add_count,
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
        .where(PublishAttempt.success.is_(True), PublishAttempt.error.is_(None))
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
    thread_url: str | None = None


async def board_queue(
    session: AsyncSession, board_name: str, settings: DashboardSettings
) -> tuple[Board | None, list[QueuedItem]]:
    board = await session.scalar(select(Board).where(Board.name == board_name))
    if board is None:
        return None, []

    now_utc = datetime.now(timezone.utc)
    fresh_cutoff = now_utc - timedelta(hours=settings.queue_fresh_window_hours)
    # SQLite stores datetimes as naive UTC; strip tzinfo for DB-level comparisons.
    fresh_cutoff_naive = fresh_cutoff.replace(tzinfo=None)

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

    _eligible = (
        SubmissionState.QUEUED.value,
        SubmissionState.PUBLISH_FAILED.value,
        SubmissionState.READY_TO_QUEUE.value,
    )

    effective_date = func.coalesce(Submission.source_posted_at, Submission.created_at)
    rows = await session.execute(
        select(
            Submission.id,
            Submission.state,
            Submission.author_display,
            Submission.embed_title,
            Submission.source_posted_at,
            Submission.created_at,
            Submission.source_discord_message_id,
            SubmissionLink.canonical_url,
            SubmissionLink.resolved_title,
            SubmissionLink.resolved_image_path,
            SubmissionLink.domain_family,
            image_count_sq.label("image_count"),
            latest_error_sq.label("error"),
            SubmissionThread.thread_id,
        )
        .outerjoin(
            SubmissionLink,
            and_(
                SubmissionLink.submission_id == Submission.id,
                SubmissionLink.order_index == 0,
            ),
        )
        .outerjoin(
            SubmissionThread,
            and_(
                SubmissionThread.board_id == Submission.board_id,
                SubmissionThread.source_discord_message_id == Submission.source_discord_message_id,
            ),
        )
        .where(Submission.board_id == board.id, Submission.state.in_(_eligible))
        .order_by(
            case(
                (effective_date >= fresh_cutoff_naive, 0),
                else_=1,
            ),
            effective_date.nulls_last(),
            Submission.created_at,
        )
    )

    items: list[QueuedItem] = []
    for r in rows:
        # Use created_at as fallback when source_posted_at is unknown (e.g. YouTube).
        effective_posted = r.source_posted_at if r.source_posted_at is not None else r.created_at
        if effective_posted is not None and effective_posted.tzinfo is None:
            effective_posted = effective_posted.replace(tzinfo=timezone.utc)
        is_fresh = effective_posted is not None and effective_posted >= fresh_cutoff
        if r.domain_family == "bluesky":
            post_type = "repost"
        elif r.resolved_image_path:
            post_type = "image"
        else:
            post_type = "link"
        thread_url = (
            f"https://discord.com/channels/{board.discord_guild_id}/{r.thread_id}"
            if r.thread_id else None
        )
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
            state=(
                "failed" if r.state == SubmissionState.PUBLISH_FAILED.value
                else "needs confirmation" if r.state == SubmissionState.READY_TO_QUEUE.value
                else "queued"
            ),
            error=r.error,
            thread_url=thread_url,
        ))
    return board, items


@dataclass
class RecentError:
    error_id: int
    source: str
    context: str
    traceback: str
    occurred_at: datetime
    occurred_rel: str


_PENDING_STATES = frozenset({
    SubmissionState.INTENT_SUBMITTED.value,
    SubmissionState.AWAITING_SOURCE.value,
    SubmissionState.AWAITING_BETTER_LINK.value,
    SubmissionState.AWAITING_IMAGE.value,
    SubmissionState.AWAITING_ALT_TEXT.value,
    SubmissionState.AWAITING_GRAPHIC_CLASSIFICATION.value,
})

_STATE_LABEL = {
    SubmissionState.INTENT_SUBMITTED.value: "submitted",
    SubmissionState.AWAITING_SOURCE.value: "needs source",
    SubmissionState.AWAITING_BETTER_LINK.value: "needs metadata",
    SubmissionState.AWAITING_IMAGE.value: "needs image",
    SubmissionState.AWAITING_ALT_TEXT.value: "needs alt text",
    SubmissionState.AWAITING_GRAPHIC_CLASSIFICATION.value: "needs classification",
}

# Ordered list of all possible blocker labels (determines display order).
_BLOCKER_ORDER = [
    "needs source",
    "needs metadata",
    "needs image",
    "needs alt text",
    "needs classification",
    "submitted",
]

# Maps open request model classes to their blocker label.
_REQUEST_BLOCKERS: list[tuple[type, str]] = [
    (SourceRequest, "needs source"),
    (MetadataRequest, "needs metadata"),
    (ImageRequest, "needs image"),
    (AttachmentAltTextRequest, "needs alt text"),
    (ContentLabelRequest, "needs classification"),
]


@dataclass
class PendingSubmission:
    submission_id: int
    board_name: str
    blockers: list[str]
    canonical_url: str | None
    author_display: str
    submitted_rel: str
    thread_url: str | None  # discord.com/channels/{guild}/{thread}


async def pending_submissions(session: AsyncSession) -> list[PendingSubmission]:
    rows = await session.execute(
        select(
            Submission.id,
            Submission.state,
            Submission.author_display,
            Submission.created_at,
            Submission.source_discord_message_id,
            Submission.board_id,
            Board.name.label("board_name"),
            Board.discord_guild_id,
            SubmissionLink.canonical_url,
            SubmissionThread.thread_id,
        )
        .join(Board, Board.id == Submission.board_id)
        .outerjoin(
            SubmissionLink,
            and_(
                SubmissionLink.submission_id == Submission.id,
                SubmissionLink.order_index == 0,
            ),
        )
        .outerjoin(
            SubmissionThread,
            and_(
                SubmissionThread.board_id == Submission.board_id,
                SubmissionThread.source_discord_message_id == Submission.source_discord_message_id,
            ),
        )
        .where(Submission.state.in_(_PENDING_STATES))
        .order_by(Submission.created_at.asc())
    )

    raw_rows = rows.all()
    if not raw_rows:
        return []

    sub_ids = [r.id for r in raw_rows]

    # Collect every open (unanswered) request type per submission.
    blocker_sets: dict[int, set[str]] = {sid: set() for sid in sub_ids}
    for model_cls, label in _REQUEST_BLOCKERS:
        open_sids = await session.scalars(
            select(model_cls.submission_id)
            .where(model_cls.submission_id.in_(sub_ids), model_cls.answered_at.is_(None))
            .distinct()
        )
        for sid in open_sids:
            blocker_sets[sid].add(label)

    result = []
    for r in raw_rows:
        thread_url = (
            f"https://discord.com/channels/{r.discord_guild_id}/{r.thread_id}"
            if r.thread_id else None
        )
        raw_blockers = blocker_sets.get(r.id, set())
        if raw_blockers:
            blockers = [b for b in _BLOCKER_ORDER if b in raw_blockers]
        else:
            # No open request rows yet - fall back to the state field.
            blockers = [_STATE_LABEL.get(r.state, r.state)]
        result.append(PendingSubmission(
            submission_id=r.id,
            board_name=r.board_name,
            blockers=blockers,
            canonical_url=r.canonical_url,
            author_display=r.author_display or "",
            submitted_rel=_relative(r.created_at),
            thread_url=thread_url,
        ))
    return result


@dataclass
class PlaylistAdd:
    board_name: str
    video_id: str
    playlist_id: str
    added_at: datetime
    added_rel: str
    success: bool
    error_message: str | None


async def recent_playlist_adds(session: AsyncSession, limit: int = 30) -> list[PlaylistAdd]:
    rows = await session.execute(
        select(
            YoutubePlaylistAdd.video_id,
            YoutubePlaylistAdd.playlist_id,
            YoutubePlaylistAdd.added_at,
            YoutubePlaylistAdd.success,
            YoutubePlaylistAdd.error_message,
            Board.name.label("board_name"),
        )
        .join(Board, Board.id == YoutubePlaylistAdd.board_id)
        .order_by(YoutubePlaylistAdd.added_at.desc())
        .limit(limit)
    )
    result = []
    for r in rows:
        added_at = r.added_at
        if added_at is not None and added_at.tzinfo is None:
            added_at = added_at.replace(tzinfo=timezone.utc)
        result.append(PlaylistAdd(
            board_name=r.board_name,
            video_id=r.video_id,
            playlist_id=r.playlist_id,
            added_at=added_at,
            added_rel=_relative(added_at),
            success=r.success,
            error_message=r.error_message,
        ))
    return result


@dataclass
class ScanInfo:
    channel_id: int
    channel_name: str
    scan_type: str  # "catchup" | "manual"
    started_rel: str


_DISCORD_THREAD_LIMIT = 1000  # active threads per guild; not exposed by Discord API


@dataclass
class GlobalStats:
    total_queued: int
    total_pending: int
    total_today: int
    active_thread_count: int
    discord_thread_limit: int
    rate_limited_until: datetime | None  # None = not currently rate limited
    rate_limited_route: str | None
    rate_limited_recently: bool          # True if rate limited within past 2 min (may have already cleared)
    bot_started_at: datetime | None
    bot_started_rel: str
    active_scans: list[ScanInfo]


async def global_stats(
    session: AsyncSession, settings: DashboardSettings, data_dir: str
) -> GlobalStats:
    import time
    from ..bot_status import read as read_bot_status

    tz = ZoneInfo(settings.queue_timezone)
    now_utc = datetime.now(timezone.utc)
    mt_midnight = _mt_midnight(now_utc, tz)
    now_ts = time.time()

    _queue_states = (
        SubmissionState.QUEUED.value,
        SubmissionState.PUBLISH_FAILED.value,
        SubmissionState.READY_TO_QUEUE.value,
    )
    total_queued = await session.scalar(
        select(func.count()).select_from(Submission).where(Submission.state.in_(_queue_states))
    ) or 0

    total_pending = await session.scalar(
        select(func.count()).select_from(Submission).where(Submission.state.in_(_PENDING_STATES))
    ) or 0

    # Prefer the Discord-fetched count written by the cleanup scheduler; fall back
    # to our DB approximation (threads not yet confirmed archived) if not yet available.
    db_thread_count = await session.scalar(
        select(func.count())
        .select_from(SubmissionThread)
        .where(SubmissionThread.archived_at.is_(None))
    ) or 0

    today_rows = await session.scalars(select(Board))
    boards = list(today_rows)
    total_today = 0
    for board in boards:
        total_today += await count_posts_today(session, board.id, mt_midnight)

    status = read_bot_status(data_dir)
    discord_thread_count = status.get("discord_active_threads")
    active_thread_count = discord_thread_count if discord_thread_count is not None else db_thread_count

    rl = status.get("rate_limit") or {}
    rate_limited_until: datetime | None = None
    rate_limited_route: str | None = None
    rate_limited_recently = False
    if rl:
        if rl.get("until", 0) > now_ts:
            rate_limited_until = datetime.fromtimestamp(rl["until"], tz=timezone.utc)
            rate_limited_route = rl.get("route")
        # Show "recently rate limited" for up to 2 minutes after it cleared.
        if rl.get("last_seen_at", 0) > now_ts - 120:
            rate_limited_recently = True
            if rate_limited_route is None:
                rate_limited_route = rl.get("route")

    bot_started_at: datetime | None = None
    started_str = status.get("started_at")
    if started_str:
        try:
            bot_started_at = datetime.fromisoformat(started_str)
        except ValueError:
            pass

    active_scans: list[ScanInfo] = []
    for s in status.get("active_scans") or []:
        started_ts = s.get("started_at")
        started_at_dt = datetime.fromtimestamp(started_ts, tz=timezone.utc) if started_ts else None
        active_scans.append(ScanInfo(
            channel_id=s.get("channel_id", 0),
            channel_name=s.get("channel_name", "?"),
            scan_type=s.get("type", "catchup"),
            started_rel=_relative(started_at_dt),
        ))

    return GlobalStats(
        total_queued=total_queued,
        total_pending=total_pending,
        total_today=total_today,
        active_thread_count=active_thread_count,
        discord_thread_limit=_DISCORD_THREAD_LIMIT,
        rate_limited_until=rate_limited_until,
        rate_limited_route=rate_limited_route,
        rate_limited_recently=rate_limited_recently,
        bot_started_at=bot_started_at,
        bot_started_rel=_relative(bot_started_at),
        active_scans=active_scans,
    )


async def recent_errors(session: AsyncSession, limit: int = 20) -> list[RecentError]:
    rows = await session.scalars(
        select(BotError).order_by(BotError.occurred_at.desc()).limit(limit)
    )
    result = []
    for r in rows:
        occurred_at = r.occurred_at
        if occurred_at is not None and occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=timezone.utc)
        result.append(RecentError(
            error_id=r.id,
            source=r.source,
            context=r.context,
            traceback=r.traceback,
            occurred_at=occurred_at,
            occurred_rel=_relative(occurred_at),
        ))
    return result
