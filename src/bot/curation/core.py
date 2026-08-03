"""Surface-agnostic curation core: submission state machine, ingest orchestration,
and the reaction/reply/button handlers.

No Discord (or any chat SDK) is imported here - the guard in
``tests/test_curation_boundary.py`` enforces it. The Discord translation layer (thread
lifecycle, ``discord.File`` builders, the butterfly entry point, the publish path) lives
in ``discord_ingest/service.py``, which re-exports these names.
"""

from __future__ import annotations
import asyncio
import contextlib
import io
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from ..accessibility import initial_alt_text, is_image_attachment, is_video_attachment
from ..asset_store import (
    StorageFullError,
    download_attachment,
    has_free_space,
    remove_submission_dir,
    submission_dir,
)
from ..canonicalize import canonicalize, is_bluesky_post_url
from ..mirrors import mirror_hint_for_url
from ..config import BoardConfig, Settings
from ..models import (
    AttachmentAltTextRequest,
    Attachment,
    Board,
    CancellationRequest,
    ConfirmationRequest,
    ContentLabelRequest,
    ImageRequest,
    MetadataRequest,
    PublishAttempt,
    SourceRequest,
    Submission,
    SubmissionLink,
    SubmissionThread,
    SupplementalImageRequest,
    SupplementalLinkRequest,
    YoutubePlaylistAdd,
)
from .. import publish as publisher
from ..moderation import (
    GRAPHIC_YES_EMOJI,
    graphic_from_emoji,
)
from ..resolve import ResolvedMetadata, resolve, resolve_bluesky_at_uri
from ..state import (
    AltTextStatus,
    GraphicStatus,
    Gap,
    PublishOutcome,
    SubmissionSnapshot,
    SubmissionState,
    evaluate_state,
    missing_gaps,
)
from .surface import NullSurface, Surface, SurfaceError
from .types import InboundAttachment, InboundMessage
from . import prompts, replies
from .events import InteractionEvent, ReactionEvent, ReplyEvent
from .outcomes import Ack, HandlerOutcome, Noop, OpenModal, Tombstone
from .urls import extract_urls, is_discord_internal_url
from .components import PreviewImage
from ..db import session_scope

log = logging.getLogger(__name__)


# Keyed by submission ID. Serializes recompute_and_request per submission so its
# read-decide-send-persist critical section stays atomic once it releases the global
# DB lock around Discord sends (docs/db-lock-io-refactor.md, surface-agnostic #50).
# Replaces the incidental mutual exclusion the global lock used to provide.
_submission_locks: dict[int, asyncio.Lock] = {}


def _submission_lock(submission_id: int) -> asyncio.Lock:
    return _submission_locks.setdefault(submission_id, asyncio.Lock())


@contextlib.asynccontextmanager
async def _maybe_submission_lock(ambient_session, submission_id: int):
    """Take the per-submission lock only for self-managing recomputes.

    Legacy in-session callers (``ambient_session`` set) already hold the global DB
    lock, which serializes them; taking the per-submission lock too would invert the
    lock order versus self-managing callers (which take per-submission then global)
    and could deadlock. So skip it for them.
    """
    if ambient_session is not None:
        yield
    else:
        async with _submission_lock(submission_id):
            yield


@contextlib.asynccontextmanager
async def _scope(ambient_session):
    """A DB scope that reuses the caller's ambient session (legacy in-session path,
    stays inside their transaction/lock) or opens a fresh short session_scope
    (self-managing path, so sends between scopes run with the lock released)."""
    if ambient_session is not None:
        yield ambient_session
    else:
        async with session_scope() as s:
            yield s


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def sync_boards(session: AsyncSession, settings: Settings) -> None:
    """Upsert board rows from config so submissions can reference them."""
    for cfg in settings.boards:
        board = await session.scalar(
            select(Board).where(Board.discord_channel_id == cfg.discord_channel_id)
        )
        if board is None:
            board = Board(
                name=cfg.name,
                discord_guild_id=cfg.discord_guild_id,
                discord_channel_id=cfg.discord_channel_id,
                nsfw=cfg.nsfw,
            )
            session.add(board)
        else:
            board.name = cfg.name
            board.discord_guild_id = cfg.discord_guild_id
            board.nsfw = cfg.nsfw


async def _board_for_channel(session: AsyncSession, channel_id: int) -> Board | None:
    return await session.scalar(
        select(Board).where(Board.discord_channel_id == channel_id)
    )


@dataclass
class TriageItem:
    thread_url: str
    title: str
    author_display: str
    state: str
    submitted_rel: str


def _triage_relative(dt: datetime | None) -> str:
    if dt is None:
        return "?"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    s = int((datetime.now(timezone.utc) - dt).total_seconds())
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


_TRIAGE_TERMINAL_STATES = {SubmissionState.PUBLISHED.value, SubmissionState.PUBLISH_FAILED.value}


# Every child table keyed by submission_id. Deleting a submission must purge all of
# these first (no DB-level cascade). Kept in one place so every delete path stays in sync.
_SUBMISSION_CHILD_MODELS = (
    SourceRequest,
    AttachmentAltTextRequest,
    ContentLabelRequest,
    ImageRequest,
    MetadataRequest,
    SupplementalImageRequest,
    SupplementalLinkRequest,
    CancellationRequest,
    ConfirmationRequest,
    PublishAttempt,
    SubmissionLink,
    Attachment,
)


async def _delete_submission_cascade(session: AsyncSession, sub_id: int) -> None:
    """Delete a submission and all of its child rows. Assumes any filesystem cleanup
    (remove_submission_dir) and Discord cleanup (thread archive) is handled by the caller."""
    for model in _SUBMISSION_CHILD_MODELS:
        await session.execute(delete(model).where(model.submission_id == sub_id))
    await session.execute(delete(Submission).where(Submission.id == sub_id))


async def fetch_triage_items(
    session: AsyncSession,
    *,
    board_id: int,
    guild_id: int,
    state_filter: str | None = None,
    user_id_filter: int | None = None,
) -> list[TriageItem]:
    """Fetch open submissions for a board, optionally filtered by state and/or submitter."""
    filters = [
        Submission.board_id == board_id,
        ~Submission.state.in_(_TRIAGE_TERMINAL_STATES),
    ]
    if state_filter is not None:
        filters.append(Submission.state == state_filter)
    if user_id_filter is not None:
        filters.append(Submission.author_id == user_id_filter)

    rows = list(await session.execute(
        select(
            Submission.id,
            Submission.state,
            Submission.author_display,
            Submission.created_at,
            Submission.thread_id,
            SubmissionLink.resolved_title,
        )
        .outerjoin(
            SubmissionLink,
            (SubmissionLink.submission_id == Submission.id)
            & (SubmissionLink.order_index == 0),
        )
        .where(*filters)
        .order_by(Submission.created_at.asc())
    ))

    items = []
    for row in rows:
        thread_url = (
            f"https://discord.com/channels/{guild_id}/{row.thread_id}"
            if row.thread_id
            else None
        )
        title = row.resolved_title or f"submission {row.id}"
        items.append(TriageItem(
            thread_url=thread_url or "",
            title=title,
            author_display=row.author_display or "unknown",
            state=row.state,
            submitted_rel=_triage_relative(row.created_at),
        ))
    return items


async def _find_prior_post(
    session: AsyncSession, canonical_url: str, exclude_submission_id: int
) -> str | None:
    """Return the bsky_url (or at_uri) of an earlier published submission with the same canonical URL."""
    attempt = await session.scalar(
        select(PublishAttempt)
        .join(Submission, PublishAttempt.submission_id == Submission.id)
        .join(SubmissionLink, SubmissionLink.submission_id == Submission.id)
        .where(
            SubmissionLink.canonical_url == canonical_url,
            PublishAttempt.success.is_(True),
            Submission.id != exclude_submission_id,
        )
        .order_by(PublishAttempt.id.desc())
        .limit(1)
    )
    if attempt is None:
        return None
    return attempt.bsky_url or attempt.at_uri


_DUPLICATE_TERMINAL_STATES = {SubmissionState.PUBLISHED.value, SubmissionState.PUBLISH_FAILED.value}


async def _find_duplicate(
    session: AsyncSession,
    canonical_url: str,
    exclude_submission_id: int,
    guild_id: int,
) -> tuple[str, str | None] | None:
    """Check whether another submission with this canonical URL is already active or posted.

    Returns ("published", bsky_url), ("queued", thread_url), ("pending", thread_url), or None.
    Published takes priority; among active states, queued takes priority over pending.
    """
    attempt = await session.scalar(
        select(PublishAttempt)
        .join(Submission, PublishAttempt.submission_id == Submission.id)
        .join(SubmissionLink, SubmissionLink.submission_id == Submission.id)
        .where(
            SubmissionLink.canonical_url == canonical_url,
            PublishAttempt.success.is_(True),
            Submission.id != exclude_submission_id,
        )
        .order_by(PublishAttempt.id.desc())
        .limit(1)
    )
    if attempt is not None:
        return "published", attempt.bsky_url or attempt.at_uri

    active = await session.scalar(
        select(Submission)
        .join(SubmissionLink, SubmissionLink.submission_id == Submission.id)
        .where(
            SubmissionLink.canonical_url == canonical_url,
            ~Submission.state.in_(_DUPLICATE_TERMINAL_STATES),
            Submission.id != exclude_submission_id,
        )
        .order_by(Submission.state == SubmissionState.QUEUED.value)  # queued first
        .limit(1)
    )
    if active is not None:
        thread_url = (
            f"https://discord.com/channels/{guild_id}/{active.thread_id}"
            if active.thread_id and guild_id
            else None
        )
        kind = "queued" if active.state == SubmissionState.QUEUED.value else "pending"
        return kind, thread_url

    return None


async def handle_reaction_removed(
    session: AsyncSession,
    event: ReactionEvent,
    surface: Surface,
    settings: Settings,
) -> None:
    """A 🦋 was removed: delete the prospective post so a re-react starts fresh.

    Only curators (by role or explicit user ID) may trigger deletion this way.
    The OP can cancel via the ❌ button in the thread instead. `surface` is the
    submission's thread (resolved by the gateway); `event.member` is resolved by the
    gateway because reaction-remove payloads carry no member.

    Deletes the submission, its links/attachments/requests, and the downloaded
    files, then posts a short notice. Re-adding 🦋 re-runs ingest via
    handle_reaction (get-or-create will create a new submission).
    """
    board = await _board_for_channel(session, event.channel_id)
    if board is None:
        return
    board_cfg = settings.board_for_channel(event.channel_id)
    if not _is_curator(event.member, event.user_id, board_cfg):
        return  # only curators can cancel via butterfly removal; OP uses the ❌ button
    submission = await session.scalar(
        select(Submission).where(
            Submission.board_id == board.id,
            Submission.source_discord_message_id == event.message_id,
        )
    )
    if submission is None:
        return  # nothing to undo

    # Block removal of already-published submissions to prevent duplicate posts.
    if submission.state == SubmissionState.PUBLISHED.value:
        attempt = await session.scalar(
            select(PublishAttempt)
            .where(PublishAttempt.submission_id == submission.id, PublishAttempt.success.is_(True))
            .order_by(PublishAttempt.attempted_at.desc())
        )
        if attempt and attempt.bsky_url:
            bsky_url = attempt.bsky_url
        elif attempt and attempt.at_uri:
            bsky_url = publisher.at_uri_to_url(attempt.at_uri, board_cfg.bluesky_handle if board_cfg else None)
        else:
            bsky_url = "Bluesky"
        await surface.send(replies.cannot_remove_published(bsky_url))
        return

    sub_id = submission.id
    remove_submission_dir(settings.attachments_dir, board.id, sub_id)
    await _delete_submission_cascade(session, sub_id)
    log.info("deleted submission %s after 🦋 removal on message %s", sub_id, event.message_id)

    # Notice goes in the thread (never the main channel). The thread is kept and
    # reused if the 🦋 is re-added, so we don't spam the channel with new threads.
    await surface.send(replies.reaction_removed())
    await surface.archive(notice=replies.closing_notice("submission removed"))


async def handle_label_reaction(
    session: AsyncSession,
    event: ReactionEvent,
    surface: Surface,
    settings: Settings,
    yt_client=None,
) -> None:
    """A curator reacted ✅/❌ on a graphic-classification request message."""
    req = await session.scalar(
        select(ContentLabelRequest).where(ContentLabelRequest.bot_message_id == event.message_id)
    )
    if req is None or req.answered_at is not None:
        return
    status = graphic_from_emoji(event.emoji)
    if status is None:
        return

    submission = await session.get(Submission, req.submission_id)
    if submission is None:
        return
    board_cfg = settings.board_for_channel(submission.channel_id)
    if not _reaction_authorized(event.member, event.user_id, submission, board_cfg):
        return

    submission.graphic_status = status.value
    req.answer = event.emoji
    req.answered_by = event.user_id
    req.answered_at = _now()
    # The reaction is on a message in the thread, so `surface` is the thread.
    await recompute_and_request(submission.id, settings=settings, destination=surface, yt_client=yt_client, ambient_session=session)


async def handle_metadata_reaction(
    session: AsyncSession,
    event: ReactionEvent,
    surface: Surface,
    settings: Settings,
    yt_client=None,
) -> None:
    """A curator reacted 🔗 on a metadata-request message - confirm this is the best link."""
    req = await session.scalar(
        select(MetadataRequest).where(
            MetadataRequest.bot_message_id == event.message_id,
            MetadataRequest.answered_at.is_(None),
        )
    )
    if req is None:
        return
    submission = await session.get(Submission, req.submission_id)
    if submission is None:
        return
    board_cfg = settings.board_for_channel(submission.channel_id)
    if not _reaction_authorized(event.member, event.user_id, submission, board_cfg):
        return

    req.answer = "confirmed"
    req.answered_by = event.user_id
    req.answered_at = _now()
    await surface.send(replies.metadata_confirmed())
    await recompute_and_request(submission.id, settings=settings, destination=surface, yt_client=yt_client, ambient_session=session)


def _gap_summary(gaps) -> str:
    """Human-readable, comma-separated list of blocking gaps for a refusal notice."""
    return ", ".join(g.value.replace("_", " ") for g in gaps)


async def handle_confirmation_reaction(
    session: AsyncSession,
    event: ReactionEvent,
    surface: Surface,
    settings: Settings,
    yt_client=None,
) -> bool:
    """A curator or OP reacted ✅ on the confirmation prompt - queue the submission."""
    req = await session.scalar(
        select(ConfirmationRequest).where(
            ConfirmationRequest.bot_message_id == event.message_id,
            ConfirmationRequest.confirmed_at.is_(None),
        )
    )
    if req is None:
        return False
    submission = await session.get(Submission, req.submission_id)
    if submission is None or submission.state in _QUEUE_TERMINAL:
        return False
    board_cfg = settings.board_for_channel(submission.channel_id)
    if not _reaction_authorized(event.member, event.user_id, submission, board_cfg):
        return False

    # Re-validate gaps at react time: a gap (e.g. alt text for a late-added image) may have
    # opened after this confirmation was posted. Refuse and refresh rather than queue blindly.
    snap, _atts, links = await _snapshot(session, submission)
    gaps = missing_gaps(snap)
    if gaps:
        log.info("refusing to queue submission %s via ✅: gaps reopened (%s)", submission.id, _gap_summary(gaps))
        await recompute_and_request(
            submission.id, settings=settings, destination=surface, yt_client=yt_client, ambient_session=session
        )
        await surface.send(replies.queue_blocked_notice(_gap_summary(gaps)))
        return False

    req.confirmed_at = _now()
    req.confirmed_by = event.user_id
    submission.state = SubmissionState.QUEUED.value
    log.info("submission %s queued by %s via ✅ confirmation", submission.id, event.user_id)
    videos_added = 0
    if not submission.playlist_skipped:
        videos_added = await _auto_add_to_playlist(
            session, submission, links, board_cfg, yt_client
        )

    queue_url = (
        f"https://dashboard.exegesis.space/boards/{board_cfg.name}" if board_cfg else None
    )
    await surface.send(replies.queued_notice(
        bluesky_handle=board_cfg.bluesky_handle if board_cfg else None,
        dashboard_url=queue_url,
        youtube_playlist_id=board_cfg.youtube_playlist_id if board_cfg else None,
        videos_added=videos_added,
    ))
    if await _playlist_close_ready(
        session, submission.board_id,
        submission.source_discord_message_id, board_cfg,
        playlist_skipped=submission.playlist_skipped,
    ):
        surface.archive_after_delay(notice=replies.closing_notice("queued"))
    return True


def _is_curator(
    member: object | None,
    user_id: int,
    board_cfg: BoardConfig | None,
) -> bool:
    """True if user_id is an explicit curator user or holds a curator role.

    ``member`` is the platform actor carried opaquely on the normalized event (a Discord
    ``Member`` today); only its ``.roles`` is read, so the check stays surface-agnostic."""
    if board_cfg is None:
        return False
    if user_id in board_cfg.curator_user_ids:
        return True
    if member is None:
        return False
    role_ids = {r.id for r in member.roles}
    return any(rid in role_ids for rid in board_cfg.curator_role_ids)


def _reaction_authorized(
    member: object | None,
    user_id: int,
    submission: Submission,
    board_cfg: BoardConfig | None,
) -> bool:
    if user_id == submission.author_id:
        return True
    return _is_curator(member, user_id, board_cfg)


async def handle_cancel_reaction(
    session: AsyncSession,
    event: ReactionEvent,
    surface: Surface,
    settings: Settings,
) -> None:
    """❌ was reacted on a cancel-request message inside a thread: delete the submission
    if authorized. The reaction is in the thread, so `surface` is the thread; it also
    clears the trigger reaction from the source post via the port."""
    req = await session.scalar(
        select(CancellationRequest).where(CancellationRequest.bot_message_id == event.message_id)
    )
    if req is None:
        return

    submission = await session.get(Submission, req.submission_id)
    if submission is None:
        return

    board_cfg = settings.board_for_channel(submission.channel_id)
    if not _reaction_authorized(event.member, event.user_id, submission, board_cfg):
        return

    if submission.state == SubmissionState.PUBLISHED.value:
        attempt = await session.scalar(
            select(PublishAttempt)
            .where(PublishAttempt.submission_id == submission.id, PublishAttempt.success.is_(True))
            .order_by(PublishAttempt.attempted_at.desc())
        )
        bsky_url = attempt.bsky_url if attempt and attempt.bsky_url else "Bluesky"
        await surface.send(replies.cannot_remove_published(bsky_url))
        return

    sub_id = submission.id
    source_channel_id = submission.channel_id
    source_message_id = submission.source_discord_message_id
    board = await session.get(Board, submission.board_id)
    remove_submission_dir(settings.attachments_dir, board.id if board else 0, sub_id)
    await _delete_submission_cascade(session, sub_id)
    log.info("deleted submission %s after ❌ cancel by user %s", sub_id, event.user_id)

    await surface.send(replies.reaction_removed())
    await surface.archive(notice=replies.closing_notice("submission cancelled"))
    await surface.clear_trigger(source_channel_id, source_message_id, settings.trigger_emoji)


async def handle_source_cancel_reaction(
    session: AsyncSession,
    event: ReactionEvent,
    surface: Surface,
    settings: Settings,
    yt_client=None,
) -> None:
    """❌ reacted on the original source post: cancel the submission and/or playlist add if OP or curator.

    `surface` is the submission's thread (resolved by the gateway), used for the
    cancellation confirmation + per-video removal notices; the trigger reaction on the
    source post is cleared through the same port."""
    board = await _board_for_channel(session, event.channel_id)
    if board is None:
        return
    board_cfg = settings.board_for_channel(event.channel_id)

    is_explicit_curator = board_cfg is not None and event.user_id in board_cfg.curator_user_ids
    is_role_curator = (
        event.member is not None
        and board_cfg is not None
        and any(r.id in board_cfg.curator_role_ids for r in event.member.roles)
    )

    cancelled_submission = False
    removed_video_ids: list[str] = []

    # Cancel any pending submission.
    submission = await session.scalar(
        select(Submission).where(
            Submission.board_id == board.id,
            Submission.source_discord_message_id == event.message_id,
        )
    )
    if submission is not None and submission.state != SubmissionState.PUBLISHED.value:
        is_op = event.user_id == submission.author_id
        if is_op or is_explicit_curator or is_role_curator:
            sub_id = submission.id
            remove_submission_dir(settings.attachments_dir, board.id, sub_id)
            await _delete_submission_cascade(session, sub_id)
            log.info("deleted submission %s after source-post ❌ by user %s", sub_id, event.user_id)
            cancelled_submission = True
            await surface.clear_trigger(event.channel_id, event.message_id, settings.trigger_emoji)

    # Cancel any playlist addition(s) for this source message.
    playlist_rows = list(await session.scalars(
        select(YoutubePlaylistAdd).where(
            YoutubePlaylistAdd.board_id == board.id,
            YoutubePlaylistAdd.source_discord_message_id == event.message_id,
            YoutubePlaylistAdd.success.is_(True),
        )
    ))
    for row in playlist_rows:
        is_requester = event.user_id == row.discord_requester_id
        if not (is_requester or is_explicit_curator or is_role_curator):
            continue
        if row.playlist_item_id and yt_client is not None:
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, yt_client.remove_from_playlist, row.playlist_item_id)
            except Exception as exc:
                log.warning("playlist remove failed for item %s: %s", row.playlist_item_id, exc)
        await session.delete(row)
        removed_video_ids.append(row.video_id)

    if not cancelled_submission and not removed_video_ids:
        return

    # Notify the thread (a no-op if the submission has no resolvable thread).
    if cancelled_submission:
        await surface.send(replies.source_cancel_confirmation(event.user_id))
        await surface.archive(notice=replies.closing_notice("submission cancelled"))
    for video_id in removed_video_ids:
        await surface.send(
            f"<@{event.user_id}> removed https://youtu.be/{video_id} "
            "from the playlist via ❌ on the source post"
        )


async def _auto_add_to_playlist(
    session: AsyncSession,
    submission: Submission,
    links: list[SubmissionLink],
    board_cfg,
    yt_client,
) -> int:
    """Auto-add any YouTube videos from submission links to the board playlist at queue time.

    Returns the number of videos successfully added.
    """
    from ..resolve.fetch import _youtube_video_id

    if yt_client is None or not board_cfg or not board_cfg.youtube_playlist_id:
        return 0

    playlist_id = board_cfg.youtube_playlist_id
    seen: set[str] = set()
    added = 0
    for link in links:
        if link.domain_family != "youtube":
            continue
        vid = _youtube_video_id(link.canonical_url)
        if not vid or vid in seen:
            continue
        seen.add(vid)

        existing = await session.scalar(
            select(YoutubePlaylistAdd).where(
                YoutubePlaylistAdd.board_id == submission.board_id,
                YoutubePlaylistAdd.video_id == vid,
                YoutubePlaylistAdd.success.is_(True),
            )
        )
        if existing is not None:
            continue

        item_id: str | None = None
        error_msg: str | None = None
        success = False
        try:
            loop = asyncio.get_running_loop()
            item_id = await loop.run_in_executor(None, yt_client.add_to_playlist, playlist_id, vid)
            success = True
            added += 1
            log.info("auto-added video %s to playlist for submission %s", vid, submission.id)
        except Exception as exc:
            error_msg = str(exc)
            log.warning("auto playlist add failed for video %s, submission %s: %s", vid, submission.id, exc)

        session.add(YoutubePlaylistAdd(
            board_id=submission.board_id,
            source_discord_message_id=submission.source_discord_message_id,
            video_id=vid,
            playlist_id=playlist_id,
            discord_requester_id=submission.author_id,
            success=success,
            error_message=error_msg,
            playlist_item_id=item_id,
        ))
    return added


async def _do_playlist_remove(
    row: YoutubePlaylistAdd,
    destination: Surface,
    session: AsyncSession,
    yt_client,
) -> None:
    """Remove a video from the YouTube playlist and clean up the DB row."""
    if row.playlist_item_id and yt_client is not None:
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, yt_client.remove_from_playlist, row.playlist_item_id)
        except Exception as exc:
            log.warning("playlist remove failed for item %s: %s", row.playlist_item_id, exc)
            await destination.send(f"failed to remove from playlist: {exc}")
            return
    await session.delete(row)
    await destination.send(f"❌ removed https://youtu.be/{row.video_id} from the playlist")


async def handle_playlist_opt_out(
    session: AsyncSession,
    event: ReactionEvent,
    surface: Surface,
    settings: Settings,
    yt_client=None,
) -> None:
    """⏹️ reacted on the playlist opt-out prompt: mark skipped and remove if already added."""
    submission = await session.scalar(
        select(Submission).where(Submission.playlist_opt_out_message_id == event.message_id)
    )
    if submission is None:
        return

    board = await session.get(Board, submission.board_id)
    board_cfg = settings.board_for_channel(board.discord_channel_id) if board else None

    is_op = event.user_id == submission.author_id
    if not (is_op or _is_curator(event.member, event.user_id, board_cfg)):
        return

    submission.playlist_skipped = True
    log.info("submission %s playlist opted out by user %s", submission.id, event.user_id)

    # Remove from playlist if auto-add already ran.
    playlist_rows = list(await session.scalars(
        select(YoutubePlaylistAdd).where(
            YoutubePlaylistAdd.board_id == submission.board_id,
            YoutubePlaylistAdd.source_discord_message_id == submission.source_discord_message_id,
            YoutubePlaylistAdd.success.is_(True),
        )
    ))
    for row in playlist_rows:
        await _do_playlist_remove(row, surface, session, yt_client)

    # If submission is QUEUED and thread is still open, it's now safe to archive.
    # (Re-arming an already-archived thread is a harmless no-op through the port.)
    if submission.state == SubmissionState.QUEUED.value and submission.thread_id:
        queued_at = submission.updated_at
        if queued_at is not None and queued_at.tzinfo is None:
            # Unreachable here in practice: playlist_skipped=True above dirties
            # the row, so autoflush fires onupdate=_utcnow (tz-aware) before this
            # read. Kept as defense in depth against future reorderings.
            queued_at = queued_at.replace(tzinfo=timezone.utc)  # pragma: no cover
        elapsed = (
            (datetime.now(timezone.utc) - queued_at).total_seconds()
            if queued_at else _THREAD_CLOSE_DELAY
        )
        remaining = max(0.0, _THREAD_CLOSE_DELAY - elapsed)
        surface.archive_after_delay(delay=remaining)


async def handle_cancel_button(
    session: AsyncSession,
    event: InteractionEvent,
    surface: Surface,
    settings: Settings,
) -> HandlerOutcome:
    """Cancel button clicked: delete the submission if authorized."""
    submission = await session.get(Submission, event.submission_id)
    if submission is None:
        return Ack("Submission not found.")

    board_cfg = settings.board_for_channel(submission.channel_id)
    if not _reaction_authorized(event.member, event.user_id, submission, board_cfg):
        return Ack("You're not authorised to cancel this submission.")

    if submission.state == SubmissionState.PUBLISHED.value:
        attempt = await session.scalar(
            select(PublishAttempt)
            .where(PublishAttempt.submission_id == submission.id, PublishAttempt.success.is_(True))
            .order_by(PublishAttempt.attempted_at.desc())
        )
        bsky_url = attempt.bsky_url if attempt and attempt.bsky_url else "Bluesky"
        return Ack(replies.cannot_remove_published(bsky_url))

    sub_id = submission.id
    source_channel_id = submission.channel_id
    source_message_id = submission.source_discord_message_id
    board = await session.get(Board, submission.board_id)
    remove_submission_dir(settings.attachments_dir, board.id if board else 0, sub_id)
    await _delete_submission_cascade(session, sub_id)
    log.info("deleted submission %s after button cancel by user %s", sub_id, event.user_id)

    await surface.send(replies.reaction_removed())
    await surface.archive(notice=replies.closing_notice("submission cancelled"))
    await surface.clear_trigger(source_channel_id, source_message_id, settings.trigger_emoji)
    return Tombstone("Submission cancelled")


async def handle_confirm_button(
    session: AsyncSession,
    event: InteractionEvent,
    surface: Surface,
    settings: Settings,
    yt_client=None,
) -> HandlerOutcome:
    """Queue for posting button clicked: queue the submission if authorized."""
    req = await session.scalar(
        select(ConfirmationRequest).where(
            ConfirmationRequest.submission_id == event.submission_id,
            ConfirmationRequest.confirmed_at.is_(None),
        )
    )
    if req is None:
        return Ack("Already queued.")

    submission = await session.get(Submission, event.submission_id)
    if submission is None or submission.state in _QUEUE_TERMINAL:
        return Ack("Nothing to queue.")

    board_cfg = settings.board_for_channel(submission.channel_id)
    if not _reaction_authorized(event.member, event.user_id, submission, board_cfg):
        return Ack("You're not authorised to queue this submission.")

    # Re-validate gaps at click time: a gap (e.g. alt text for a late-added image) may have
    # opened after this button was posted. Refuse and refresh rather than queue blindly.
    snap, _atts, links = await _snapshot(session, submission)
    gaps = missing_gaps(snap)
    if gaps:
        log.info("refusing to queue submission %s via button: gaps reopened (%s)", submission.id, _gap_summary(gaps))
        await recompute_and_request(
            submission.id, settings=settings, destination=surface, yt_client=yt_client, ambient_session=session
        )
        return Ack(
            f"Can't queue yet - still needs: {_gap_summary(gaps)}. See the checklist in this thread."
        )

    req.confirmed_at = _now()
    req.confirmed_by = event.user_id
    submission.state = SubmissionState.QUEUED.value
    log.info("submission %s queued by %s via button", submission.id, event.user_id)

    videos_added = 0
    if not submission.playlist_skipped:
        videos_added = await _auto_add_to_playlist(session, submission, links, board_cfg, yt_client)

    queue_url = (
        f"https://dashboard.exegesis.space/boards/{board_cfg.name}" if board_cfg else None
    )
    await surface.send(replies.queued_notice(
        bluesky_handle=board_cfg.bluesky_handle if board_cfg else None,
        dashboard_url=queue_url,
        youtube_playlist_id=board_cfg.youtube_playlist_id if board_cfg else None,
        videos_added=videos_added,
    ))
    if await _playlist_close_ready(
        session, submission.board_id,
        submission.source_discord_message_id, board_cfg,
        playlist_skipped=submission.playlist_skipped,
    ):
        surface.archive_after_delay(notice=replies.closing_notice("queued"))
    return Tombstone("Queued ✅")


async def handle_metadata_confirm_button(
    session: AsyncSession,
    event: InteractionEvent,
    surface: Surface,
    settings: Settings,
    yt_client=None,
) -> HandlerOutcome:
    """Use link as-is button clicked: confirm current link metadata."""
    req = await session.scalar(
        select(MetadataRequest).where(
            MetadataRequest.submission_id == event.submission_id,
            MetadataRequest.answered_at.is_(None),
        )
    )
    if req is None:
        return Ack("Already confirmed.")

    submission = await session.get(Submission, event.submission_id)
    if submission is None:
        return Noop()

    board_cfg = settings.board_for_channel(submission.channel_id)
    if not _reaction_authorized(event.member, event.user_id, submission, board_cfg):
        return Ack("You're not authorised to confirm this.")

    req.answer = "confirmed"
    req.answered_by = event.user_id
    req.answered_at = _now()

    await surface.send(replies.metadata_confirmed())
    await recompute_and_request(submission.id, settings=settings, destination=surface, yt_client=yt_client, ambient_session=session)
    return Tombstone("Link confirmed 🔗")


async def handle_graphic_button(
    session: AsyncSession,
    event: InteractionEvent,
    surface: Surface,
    settings: Settings,
    yt_client=None,
) -> HandlerOutcome:
    """Mark as graphic content button clicked."""
    req = await session.scalar(
        select(ContentLabelRequest).where(
            ContentLabelRequest.submission_id == event.submission_id,
            ContentLabelRequest.answered_at.is_(None),
        )
    )
    if req is None:
        return Ack("Already classified.")

    submission = await session.get(Submission, event.submission_id)
    if submission is None:
        return Noop()

    board_cfg = settings.board_for_channel(submission.channel_id)
    if not _reaction_authorized(event.member, event.user_id, submission, board_cfg):
        return Ack("You're not authorised to classify this.")

    submission.graphic_status = GraphicStatus.GRAPHIC.value
    req.answer = GRAPHIC_YES_EMOJI
    req.answered_by = event.user_id
    req.answered_at = _now()

    await recompute_and_request(submission.id, settings=settings, destination=surface, yt_client=yt_client, ambient_session=session)
    return Tombstone("Marked as graphic 🩸")


async def waive_source(
    session: AsyncSession,
    submission: Submission,
    *,
    settings: Settings,
    user_id: int,
    destination: Surface,
    yt_client=None,
) -> bool:
    """Mark a submission as having no known source (the `/nosource` action).

    Caller is responsible for authorization. Returns False if already waived (no-op),
    True after waiving. Posts the "source unknown" notice and recomputes state.
    """
    if submission.source_waived:
        return False
    submission.source_waived = True
    req = await session.scalar(
        select(SourceRequest).where(
            SourceRequest.submission_id == submission.id,
            SourceRequest.answered_at.is_(None),
        )
    )
    if req is not None:
        req.answer = "no_source"
        req.answered_by = user_id
        req.answered_at = _now()
    await destination.send(replies.no_source_marked())
    await recompute_and_request(submission.id, settings=settings, destination=destination, yt_client=yt_client, ambient_session=session)
    return True


async def skip_all_alt_text(
    session: AsyncSession,
    submission: Submission,
    *,
    settings: Settings,
    user_id: int,
    destination: Surface,
    yt_client=None,
) -> int:
    """Skip alt text for every image/video still needing it (the `/skipalt` action).

    Caller is responsible for authorization. Returns the number of attachments skipped
    (0 = nothing pending, no notice posted). Per-image editing stays available via the
    Edit alt text modal/picker.
    """
    pending = list(
        await session.scalars(
            select(Attachment).where(
                Attachment.submission_id == submission.id,
                Attachment.alt_text_status == AltTextStatus.NEEDED.value,
            )
        )
    )
    if not pending:
        return 0
    for att in pending:
        att.alt_text_status = AltTextStatus.SKIPPED.value
        att.alt_text_author = user_id
    reqs = await session.scalars(
        select(AttachmentAltTextRequest).where(
            AttachmentAltTextRequest.submission_id == submission.id,
            AttachmentAltTextRequest.attachment_id.in_([a.id for a in pending]),
            AttachmentAltTextRequest.answered_at.is_(None),
        )
    )
    for req in reqs:
        req.answer = "skipped"
        req.answered_by = user_id
        req.answered_at = _now()
    await destination.send(replies.alt_text_skipped_all(len(pending)))
    await recompute_and_request(submission.id, settings=settings, destination=destination, yt_client=yt_client, ambient_session=session)
    return len(pending)


# Max length of a free-text non-URL source note (a citation, not an essay).
_SOURCE_NOTE_MAX = 300


async def handle_source_note_confirm(
    session: AsyncSession,
    event: InteractionEvent,
    surface: Surface,
    settings: Settings,
    yt_client=None,
) -> HandlerOutcome:
    """Confirm-button on the 'that's not a URL - use it as the source?' prompt.

    Commits the pending source_note so it counts as the source (OP or curator).
    """
    submission = await session.get(Submission, event.submission_id)
    if submission is None:
        return Noop()
    board_cfg = settings.board_for_channel(submission.channel_id)
    if not _reaction_authorized(event.member, event.user_id, submission, board_cfg):
        return Ack("You're not authorised to mark this.")
    if not submission.source_note:
        return Ack("Nothing to confirm - reply with the source first.")

    submission.source_note_confirmed = True
    # Close any open source request - the note satisfies it.
    req = await session.scalar(
        select(SourceRequest).where(
            SourceRequest.submission_id == submission.id,
            SourceRequest.answered_at.is_(None),
        )
    )
    if req is not None:
        req.answer = "source_note"
        req.answered_by = event.user_id
        req.answered_at = _now()

    await surface.send(replies.source_note_confirmed(submission.source_note))
    await recompute_and_request(submission.id, settings=settings, destination=surface, yt_client=yt_client, ambient_session=session)
    return Tombstone("Source noted 📄")


async def handle_source_note_reject(
    session: AsyncSession,
    event: InteractionEvent,
    surface: Surface,
    settings: Settings,
    yt_client=None,
) -> HandlerOutcome:
    """Cancel-button on the source-note prompt: discard the pending note and re-prompt."""
    submission = await session.get(Submission, event.submission_id)
    if submission is None:
        return Noop()
    board_cfg = settings.board_for_channel(submission.channel_id)
    if not _reaction_authorized(event.member, event.user_id, submission, board_cfg):
        return Ack("You're not authorised to mark this.")

    submission.source_note = None
    submission.source_note_confirmed = False
    await surface.send(replies.source_note_rejected())
    await recompute_and_request(submission.id, settings=settings, destination=surface, yt_client=yt_client, ambient_session=session)
    return Tombstone("Discarded 🗑️")


async def handle_playlist_skip_button(
    session: AsyncSession,
    event: InteractionEvent,
    surface: Surface,
    settings: Settings,
    yt_client=None,
) -> HandlerOutcome:
    """Skip playlist button clicked: opt out and remove if already added."""
    submission = await session.get(Submission, event.submission_id)
    if submission is None:
        return Ack("Submission not found.")

    if submission.playlist_skipped:
        return Ack("Already opted out.")

    board = await session.get(Board, submission.board_id)
    board_cfg = settings.board_for_channel(board.discord_channel_id) if board else None
    is_op = event.user_id == submission.author_id
    if not (is_op or _is_curator(event.member, event.user_id, board_cfg)):
        return Ack("You're not authorised to skip the playlist.")

    submission.playlist_skipped = True
    log.info("submission %s playlist opted out via button by user %s", submission.id, event.user_id)

    playlist_rows = list(await session.scalars(
        select(YoutubePlaylistAdd).where(
            YoutubePlaylistAdd.board_id == submission.board_id,
            YoutubePlaylistAdd.source_discord_message_id == submission.source_discord_message_id,
            YoutubePlaylistAdd.success.is_(True),
        )
    ))
    for row in playlist_rows:
        await _do_playlist_remove(row, surface, session, yt_client)

    # If it was already queued, the thread is on a close timer; opting out shouldn't
    # reset it. Re-arm the archive for the time that was left (0 = close now). The
    # surface is the submission's thread, so no separate thread lookup is needed.
    if submission.state == SubmissionState.QUEUED.value and submission.thread_id:
        queued_at = submission.updated_at
        if queued_at is not None and queued_at.tzinfo is None:
            queued_at = queued_at.replace(tzinfo=timezone.utc)
        elapsed = (
            (datetime.now(timezone.utc) - queued_at).total_seconds()
            if queued_at else _THREAD_CLOSE_DELAY
        )
        remaining = max(0.0, _THREAD_CLOSE_DELAY - elapsed)
        surface.archive_after_delay(delay=remaining)

    return Tombstone("Playlist skipped ⏹️")


async def handle_edit_button(
    session: AsyncSession,
    event: InteractionEvent,
    settings: Settings,
) -> HandlerOutcome:
    """Edit button clicked: open a modal to update the post text."""
    submission = await session.get(Submission, event.submission_id)
    if submission is None:
        return Ack("Submission not found.")

    board_cfg = settings.board_for_channel(submission.channel_id)
    if not _reaction_authorized(event.member, event.user_id, submission, board_cfg):
        return Ack("You're not authorised to edit this submission.")

    primary = await session.scalar(
        select(SubmissionLink)
        .where(SubmissionLink.submission_id == event.submission_id, SubmissionLink.order_index == 0)
    )
    media = await _media_attachments(session, event.submission_id)
    return OpenModal(prompts.post_edit_modal(
        submission_id=event.submission_id,
        current_title=primary.resolved_title if primary else None,
        media=[(a.id, a.filename, a.alt_text_body) for a in media[:4]],
    ))


async def handle_alt_edit_button(
    session: AsyncSession,
    event: InteractionEvent,
    settings: Settings,
    yt_client=None,
) -> HandlerOutcome:
    """Edit-alt-text button: show an image picker (for posts with more media than the modal fits)."""
    submission = await session.get(Submission, event.submission_id)
    if submission is None:
        return Ack("Submission not found.")
    board_cfg = settings.board_for_channel(submission.channel_id)
    if not _reaction_authorized(event.member, event.user_id, submission, board_cfg):
        return Ack("You're not authorised to edit this submission.")
    media = await _media_attachments(session, event.submission_id)
    if not media:
        return Ack("This post has no images to edit.")
    if len(media) > 25:
        log.warning("submission %s has %d media; alt picker shows the first 25", event.submission_id, len(media))
    return Ack(
        "Pick an image to edit its alt text:",
        components=prompts.alt_picker_components(event.submission_id, [(a.id, a.filename) for a in media]),
    )


async def handle_alt_pick(
    session: AsyncSession,
    event: InteractionEvent,
    settings: Settings,
    yt_client=None,
) -> HandlerOutcome:
    """Image picked from the alt picker: open that attachment's alt-text modal."""
    if not event.values:
        return Noop()
    attachment_id = int(event.values[0])
    submission = await session.get(Submission, event.submission_id)
    att = await session.get(Attachment, attachment_id)
    board_cfg = settings.board_for_channel(submission.channel_id) if submission else None
    if submission is None or not _reaction_authorized(event.member, event.user_id, submission, board_cfg):
        return Ack("You're not authorised to edit this submission.")
    if att is None or att.submission_id != event.submission_id:
        return Ack("Image not found.")
    return OpenModal(prompts.alt_edit_modal(attachment_id, att.filename, att.alt_text_body))


async def _media_attachments(session: AsyncSession, submission_id: int) -> list[Attachment]:
    """A submission's image/video attachments, ordered by id (stable insertion order)."""
    rows = list(await session.scalars(
        select(Attachment)
        .where(Attachment.submission_id == submission_id)
        .order_by(Attachment.id)
    ))
    return [a for a in rows if a.is_image or a.is_video]


def _set_attachment_alt(att: Attachment, value: str | None, editor_id: int) -> None:
    """Write alt text onto an attachment. Non-empty -> PROVIDED; empty -> SKIPPED (an
    explicit clear that keeps the post queueable). Stamps the editor as the author."""
    text = (value or "").strip()
    if text:
        att.alt_text_body = text
        att.alt_text_status = AltTextStatus.PROVIDED.value
    else:
        att.alt_text_body = None
        att.alt_text_status = AltTextStatus.SKIPPED.value
    att.alt_text_author = editor_id


async def apply_post_edits(
    session: AsyncSession,
    *,
    submission_id: int,
    new_title: str,
    alt_updates: dict[int, str] | None = None,
    edited_by: int = 0,
) -> None:
    """Apply curator edits: the post text (resolved_title on primary link) and, optionally,
    per-image alt text keyed by attachment id."""
    primary = await session.scalar(
        select(SubmissionLink)
        .where(SubmissionLink.submission_id == submission_id, SubmissionLink.order_index == 0)
    )
    if primary is not None:
        primary.resolved_title = new_title.strip() or None
    for att_id, value in (alt_updates or {}).items():
        att = await session.get(Attachment, att_id)
        if att is not None and att.submission_id == submission_id:
            _set_attachment_alt(att, value, edited_by)
    log.info("applied post edit for submission %s (%d alt field(s))", submission_id, len(alt_updates or {}))


async def apply_single_alt(
    session: AsyncSession,
    *,
    attachment_id: int,
    value: str,
    edited_by: int,
) -> None:
    """Apply alt text to one attachment (from the per-image picker modal)."""
    att = await session.get(Attachment, attachment_id)
    if att is not None:
        _set_attachment_alt(att, value, edited_by)
        log.info("applied alt-text edit for attachment %s", attachment_id)


@dataclass
class _AttachmentPlan:
    """A persisted Attachment skeleton row awaiting its downloaded file."""
    row_id: int
    url: str
    filename: str
    is_video: bool


@dataclass
class _LinkPlan:
    """A persisted SubmissionLink skeleton row awaiting resolution."""
    link_id: int
    canonical_url: str
    domain_family: str
    is_primary: bool


@dataclass
class _IngestPlan:
    """Skeleton rows + context captured under the lock, fed to the (lockless)
    download/resolve gather phase."""
    submission_id: int
    board_id: int
    link_plans: list[_LinkPlan]
    att_plans: list[_AttachmentPlan]
    embed_title: str | None
    embed_description: str | None
    embed_thumb_url: str | None
    thumb_proxy_url: str | None
    # A video attachment already exists (Discord-uploaded here, or a resolver
    # video preserved across reingest) - suppresses re-downloading a resolved one.
    has_existing_video: bool


@dataclass
class _LinkOutcome:
    link_id: int
    title: str | None
    description: str | None
    image_url: str | None
    via: str | None
    source_at_uri: str | None
    image_path: str | None
    video_url: str | None = None
    video_width: int | None = None
    video_height: int | None = None
    video_path: str | None = None


@dataclass
class _IngestOutcome:
    """Results of the gather phase, written back to the rows under the lock."""
    link_outcomes: list[_LinkOutcome]
    att_paths: dict[int, str]


def _extract_raw_urls(message: InboundMessage) -> list[str]:
    """Candidate source URLs from message text, then embed URLs (mobile share
    sheets), then forwarded-snapshot content/embeds - first non-empty wins."""
    # Drop Discord navigation links (jump/message/channel/invite) at every stage:
    # they show up when a message quotes or forwards another Discord message, and
    # are never the actual source. Filtering per-stage (not just at the end) lets a
    # message whose text is ONLY a jump link fall through to the forwarded snapshot
    # that carries the real source URL.
    raw_urls = [u for u in extract_urls(message.content) if not is_discord_internal_url(u)]
    if not raw_urls:
        seen: set[str] = set()
        for embed in message.embeds:
            if embed.url and embed.url not in seen and not is_discord_internal_url(embed.url):
                seen.add(embed.url)
                raw_urls.append(embed.url)
    if not raw_urls:
        for snap in message.snapshots:
            snap_urls = [u for u in extract_urls(snap.content or "") if not is_discord_internal_url(u)]
            if snap_urls:
                raw_urls.extend(snap_urls)
                break
            for embed in snap.embeds:
                if embed.url and not is_discord_internal_url(embed.url):
                    raw_urls.append(embed.url)
                    break
            if raw_urls:
                break
    return raw_urls


async def _persist_ingest_skeletons(
    session: AsyncSession, submission: Submission, message: InboundMessage
) -> _IngestPlan:
    """Create the SubmissionLink + Attachment skeleton rows (no downloads) and
    capture the embed. DB only, so it runs under a short lock; the returned plan
    is what the lockless gather phase downloads/resolves against."""
    for i, raw in enumerate(_extract_raw_urls(message)):
        res = canonicalize(raw)
        session.add(SubmissionLink(
            submission_id=submission.id, order_index=i,
            raw_url=raw, canonical_url=res.canonical_url, domain_family=res.domain_family,
        ))
    await session.flush()
    links = list((await session.scalars(
        select(SubmissionLink)
        .where(SubmissionLink.submission_id == submission.id)
        .order_by(SubmissionLink.order_index)
    )).all())
    link_plans = [
        _LinkPlan(link_id=link.id, canonical_url=link.canonical_url,
                  domain_family=link.domain_family, is_primary=(idx == 0))
        for idx, link in enumerate(links)
    ]

    thumb_proxy_url = _capture_embed(submission, message)

    all_attachments = list(message.attachments)
    for snap in message.snapshots:
        all_attachments.extend(snap.attachments)
    created: list[tuple[Attachment, InboundAttachment, bool]] = []
    for att in all_attachments:
        is_img = is_image_attachment(att.content_type, att.filename)
        is_vid = is_video_attachment(att.content_type, att.filename)
        status, body = initial_alt_text(is_image=is_img, is_video=is_vid, discord_description=att.description)
        row = Attachment(
            submission_id=submission.id, discord_attachment_id=att.id, filename=att.filename,
            discord_url=att.url, mime=att.content_type, width=att.width, height=att.height,
            spoiler=att.spoiler, is_image=is_img, is_video=is_vid,
            alt_text_status=status.value, alt_text_body=body,
        )
        session.add(row)
        created.append((row, att, is_vid))
    await session.flush()
    att_plans = [
        _AttachmentPlan(row_id=row.id, url=att.url, filename=att.filename, is_video=is_vid)
        for row, att, is_vid in created
    ]

    # Covers both a Discord video just created above and a resolver video preserved
    # across reingest (discord_attachment_id == 0), so gather won't re-download one.
    has_existing_video = await session.scalar(
        select(Attachment.id).where(
            Attachment.submission_id == submission.id, Attachment.is_video.is_(True)
        )
    ) is not None

    return _IngestPlan(
        submission_id=submission.id, board_id=submission.board_id,
        link_plans=link_plans, att_plans=att_plans,
        embed_title=submission.embed_title, embed_description=submission.embed_description,
        embed_thumb_url=submission.embed_thumb_url, thumb_proxy_url=thumb_proxy_url,
        has_existing_video=has_existing_video,
    )


async def _download_attachment_file(
    plan: _AttachmentPlan, dest: str, settings: Settings, http_client: httpx.AsyncClient
) -> str | None:
    """Download one attachment's bytes (transcoding video). HTTP only; returns the
    local path, or None on failure/storage-full."""
    try:
        path = await download_attachment(
            url=plan.url, dest_dir=dest, filename=f"{plan.row_id}_{plan.filename}",
            data_dir=settings.data_dir, min_free_mb=settings.storage_min_free_mb, client=http_client,
        )
    except StorageFullError:
        log.warning("storage full: attachment row %s not downloaded", plan.row_id)
        return None
    except (httpx.HTTPError, OSError) as exc:
        log.warning("failed to download attachment row %s: %s", plan.row_id, exc)
        return None
    if plan.is_video and path:
        path = await _transcode_video(path)
    return path


async def _download_thumb(
    plan: _IngestPlan, link: _LinkPlan, image_url: str, dest: str,
    settings: Settings, http_client: httpx.AsyncClient,
) -> str | None:
    """Download a link's resolved thumbnail, falling back to the Discord proxy
    copy for the primary link when the source CDN blocks us. HTTP only."""
    extra_headers: dict[str, str] = {}
    if "upload.wikimedia.org" in image_url:
        from ..resolve.fetch import _UA as _RESOLVE_UA
        extra_headers["Referer"] = "https://en.wikipedia.org/"
        extra_headers["User-Agent"] = _RESOLVE_UA
    try:
        return await download_attachment(
            url=image_url, dest_dir=dest, filename=f"thumb_{link.link_id}",
            data_dir=settings.data_dir, min_free_mb=settings.storage_min_free_mb,
            client=http_client, headers=extra_headers or None,
        )
    except (StorageFullError, httpx.HTTPError, OSError) as exc:
        log.info("thumbnail download failed for link %s: %s", link.link_id, exc)
        # If the original URL failed and we have a Discord proxy copy, try that
        # (sites like FurAffinity whose CDN requires auth/Referer).
        if link.is_primary and plan.thumb_proxy_url and plan.thumb_proxy_url != image_url:
            try:
                path = await download_attachment(
                    url=plan.thumb_proxy_url, dest_dir=dest, filename=f"thumb_{link.link_id}",
                    data_dir=settings.data_dir, min_free_mb=settings.storage_min_free_mb, client=http_client,
                )
                log.info("thumbnail downloaded via Discord proxy for link %s", link.link_id)
                return path
            except (StorageFullError, httpx.HTTPError, OSError) as exc2:
                log.info("Discord proxy thumbnail also failed for link %s: %s", link.link_id, exc2)
        return None


async def _download_resolved_video(
    link: _LinkPlan, meta: ResolvedMetadata, dest: str, settings: Settings, http_client: httpx.AsyncClient
) -> str | None:
    """Download (and transcode) a resolver-provided video for a link. HTTP only;
    returns the local path, or None on failure/oversize so ingest degrades to the
    thumbnail card."""
    filename = f"linkvid_{link.link_id}.mp4"
    if meta.video_is_stream:
        # Stream manifest (e.g. reddit): ffmpeg fetches and muxes video + audio.
        path = await _fetch_stream_video(meta.video_url, dest, filename, settings)
        if path is None:
            log.info("resolved stream video failed for link %s - falling back to thumbnail", link.link_id)
            return None
    else:
        try:
            path = await download_attachment(
                url=meta.video_url, dest_dir=dest, filename=filename,
                data_dir=settings.data_dir, min_free_mb=settings.storage_min_free_mb, client=http_client,
            )
        except (StorageFullError, httpx.HTTPError, OSError) as exc:
            log.info("resolved video download failed for link %s: %s - falling back to thumbnail", link.link_id, exc)
            return None
        path = await _transcode_video(path)
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    if size > _MAX_RESOLVED_VIDEO_BYTES:
        log.info("resolved video for link %s is %d bytes (over %d limit) - falling back to thumbnail",
                 link.link_id, size, _MAX_RESOLVED_VIDEO_BYTES)
        return None
    return path


async def _gather_ingest(
    plan: _IngestPlan, settings: Settings, http_client: httpx.AsyncClient
) -> _IngestOutcome:
    """Download attachments and resolve link metadata/media. **No DB access** - runs
    with the lock released (see docs/db-lock-io-refactor.md)."""
    dest = submission_dir(settings.attachments_dir, plan.board_id, plan.submission_id)

    att_paths: dict[int, str] = {}
    for ap in plan.att_plans:
        path = await _download_attachment_file(ap, dest, settings, http_client)
        if path is not None:
            att_paths[ap.row_id] = path

    link_outcomes: list[_LinkOutcome] = []
    for lp in plan.link_plans:
        meta = await resolve(
            lp.canonical_url, lp.domain_family, client=http_client,
            fallback_title=plan.embed_title if lp.is_primary else None,
            fallback_description=plan.embed_description if lp.is_primary else None,
            fallback_image_url=plan.embed_thumb_url if lp.is_primary else None,
            youtube_api_key=settings.youtube_api_key,
        )
        # Pin the permanent DID now, while the source handle still resolves.
        source_at_uri = (
            await resolve_bluesky_at_uri(lp.canonical_url, http_client)
            if lp.domain_family == "bluesky" else None
        )
        image_path = (
            await _download_thumb(plan, lp, meta.image_url, dest, settings, http_client)
            if meta.image_url else None
        )
        outcome = _LinkOutcome(
            link_id=lp.link_id, title=meta.title, description=meta.description,
            image_url=meta.image_url, via=meta.via, source_at_uri=source_at_uri, image_path=image_path,
        )
        if lp.is_primary and meta.video_url and not plan.has_existing_video:
            video_path = await _download_resolved_video(lp, meta, dest, settings, http_client)
            if video_path is not None:
                outcome.video_url = meta.video_url
                outcome.video_width = meta.video_width
                outcome.video_height = meta.video_height
                outcome.video_path = video_path
        link_outcomes.append(outcome)
    return _IngestOutcome(link_outcomes=link_outcomes, att_paths=att_paths)


async def _attach_resolved_video(
    session: AsyncSession, submission_id: int, link_id: int, *,
    video_url: str | None, video_width: int | None, video_height: int | None, video_path: str,
) -> bool:
    """Create the resolver-sourced video Attachment row, unless the submission
    already has a video (Discord-uploaded or a prior resolve). Returns True if a
    row was created. Shared by ingest and the video backfill admin script."""
    existing = await session.scalar(
        select(Attachment.id).where(
            Attachment.submission_id == submission_id, Attachment.is_video.is_(True)
        )
    )
    if existing is not None:
        return False
    status, body = initial_alt_text(is_image=False, is_video=True, discord_description=None)
    session.add(Attachment(
        submission_id=submission_id, discord_attachment_id=0,
        filename=f"linkvid_{link_id}.mp4", discord_url=video_url, mime="video/mp4",
        width=video_width, height=video_height, is_image=False, is_video=True,
        alt_text_status=status.value, alt_text_body=body,
        local_path=video_path, downloaded_at=_now(),
    ))
    log.info("resolved video attached for submission %s (link %s)", submission_id, link_id)
    return True


async def _persist_ingest_outcome(
    session: AsyncSession, outcome: _IngestOutcome, submission_id: int
) -> None:
    """Write gathered download paths + resolved metadata back onto the skeleton
    rows, attaching any resolved video. DB only."""
    for row_id, path in outcome.att_paths.items():
        att = await session.get(Attachment, row_id)
        if att is not None:
            att.local_path = path
            att.downloaded_at = _now()
    for lo in outcome.link_outcomes:
        link = await session.get(SubmissionLink, lo.link_id)
        if link is None:
            continue
        link.resolved_title = lo.title
        link.resolved_description = lo.description
        link.resolved_image_url = lo.image_url
        link.resolved_via = lo.via
        # Cleared for non-bluesky links so a re-resolve after an edit can't leave
        # a stale URI behind.
        link.source_at_uri = lo.source_at_uri
        link.resolved_image_path = lo.image_path
        if lo.video_path is not None:
            await _attach_resolved_video(
                session, submission_id, lo.link_id,
                video_url=lo.video_url, video_width=lo.video_width,
                video_height=lo.video_height, video_path=lo.video_path,
            )


async def ingest_message_content(
    settings: Settings, inbound: InboundMessage, submission_id: int, http_client: httpx.AsyncClient
) -> None:
    """Ingest a submission's links, embed, attachments, and resolved media,
    keeping all HTTP (metadata resolution and thumbnail/attachment/video
    downloads) out of the DB lock (see docs/db-lock-io-refactor.md).

    Self-managing: a short DB scope persists the skeleton rows, a lockless gather
    phase does the downloads/resolution, then a short DB scope writes the results.
    Callers must NOT hold a session_scope. ``inbound`` is the surface-agnostic message
    (the Discord caller converts via ``discord_message_to_inbound``).
    """
    async with session_scope() as session:
        submission = await session.get(Submission, submission_id)
        if submission is None:
            return
        plan = await _persist_ingest_skeletons(session, submission, inbound)
    outcome = await _gather_ingest(plan, settings, http_client)
    async with session_scope() as session:
        await _persist_ingest_outcome(session, outcome, submission_id)


async def _ingest_attachment_in_session(
    session: AsyncSession, submission: Submission, att: InboundAttachment,
    settings: Settings, http_client: httpx.AsyncClient,
) -> None:
    """Create one Attachment row and download its file, in the caller's session.

    Used by the supplemental-content reply handlers, which still hold the DB lock
    during this download; their lockless migration is part of the handler-cascade
    slice (docs/db-lock-io-refactor.md)."""
    is_img = is_image_attachment(att.content_type, att.filename)
    is_vid = is_video_attachment(att.content_type, att.filename)
    status, body = initial_alt_text(is_image=is_img, is_video=is_vid, discord_description=att.description)
    row = Attachment(
        submission_id=submission.id, discord_attachment_id=att.id, filename=att.filename,
        discord_url=att.url, mime=att.content_type, width=att.width, height=att.height,
        spoiler=att.spoiler, is_image=is_img, is_video=is_vid,
        alt_text_status=status.value, alt_text_body=body,
    )
    session.add(row)
    await session.flush()  # assign row.id
    dest = submission_dir(settings.attachments_dir, submission.board_id, submission.id)
    path = await _download_attachment_file(
        _AttachmentPlan(row_id=row.id, url=att.url, filename=att.filename, is_video=is_vid),
        dest, settings, http_client,
    )
    if path is not None:
        row.local_path = path
        row.downloaded_at = _now()


async def _resolve_links_in_session(
    session: AsyncSession, submission: Submission, settings: Settings, http_client: httpx.AsyncClient,
) -> None:
    """Re-resolve a submission's existing links in the caller's session (still under
    the DB lock). Used by the supplemental-content reply handlers; migrating them to
    the lockless path is part of the handler-cascade slice (docs/db-lock-io-refactor.md)."""
    links = list((await session.scalars(
        select(SubmissionLink)
        .where(SubmissionLink.submission_id == submission.id)
        .order_by(SubmissionLink.order_index)
    )).all())
    if not links:
        return
    has_existing_video = await session.scalar(
        select(Attachment.id).where(
            Attachment.submission_id == submission.id, Attachment.is_video.is_(True)
        )
    ) is not None
    plan = _IngestPlan(
        submission_id=submission.id, board_id=submission.board_id,
        link_plans=[
            _LinkPlan(link_id=link.id, canonical_url=link.canonical_url,
                      domain_family=link.domain_family, is_primary=(idx == 0))
            for idx, link in enumerate(links)
        ],
        att_plans=[], embed_title=submission.embed_title,
        embed_description=submission.embed_description, embed_thumb_url=submission.embed_thumb_url,
        thumb_proxy_url=None, has_existing_video=has_existing_video,
    )
    outcome = await _gather_ingest(plan, settings, http_client)
    await _persist_ingest_outcome(session, outcome, submission.id)


def _capture_embed(submission: Submission, message: InboundMessage) -> str | None:
    """Store the link embed's title/description/thumb on the submission.

    Drives the external-embed preview and the at-least-one-image check. Embeds
    may be absent if ingestion ran before Discord had time to generate them.
    Also checks forwarded-message snapshots.

    Returns the thumbnail proxy_url (if any) as a download fallback for callers.
    """
    all_embeds = list(message.embeds)
    for snap in message.snapshots:
        all_embeds.extend(snap.embeds)
    for embed in all_embeds:
        thumb = embed.thumbnail_url or embed.image_url
        thumb_proxy = embed.thumbnail_proxy_url or embed.image_proxy_url or None
        if embed.title or embed.description or thumb:
            submission.embed_title = embed.title
            submission.embed_description = embed.description
            submission.embed_thumb_url = thumb
            return thumb_proxy
    return None


# Matches the publish-side Bluesky video limit (95 MB headroom under 100 MB).
# Oversize resolved videos are dropped here so the submission falls back to the
# thumbnail card instead of queueing a video that can never upload.
_MAX_RESOLVED_VIDEO_BYTES = 95 * 1024 * 1024


_STREAM_MUX_TIMEOUT = 180  # seconds; ffmpeg fetch+mux of a remote HLS/DASH stream


async def _fetch_stream_video(
    manifest_url: str, dest_dir: str, filename: str, settings: Settings
) -> str | None:
    """Fetch a remote HLS/DASH stream and mux video + audio into an H.264/AAC MP4.

    Used for sources (reddit) that serve video and audio as separate streams: ffmpeg
    reads the manifest, picks the best video+audio, and writes one file. Returns the
    output path, or None on any failure (no space, timeout, ffmpeg error) so ingest
    degrades to the thumbnail card.
    """
    if not has_free_space(settings.data_dir, settings.storage_min_free_mb):
        log.info("no free space for stream video %s", manifest_url)
        return None
    out_path = os.path.join(dest_dir, filename)
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-i", manifest_url,
            "-c:v", "libx264", "-c:a", "aac", "-movflags", "+faststart",
            "-y", out_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        log.warning("could not start ffmpeg for stream %s: %s", manifest_url, exc)
        return None
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_STREAM_MUX_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        log.warning("ffmpeg stream mux timed out for %s", manifest_url)
        return None
    if proc.returncode != 0:
        log.warning("ffmpeg stream mux failed for %s: %s", manifest_url, stderr.decode()[-500:])
        return None
    return out_path


async def _transcode_video(input_path: str) -> str:
    """Transcode a video to H.264 + AAC MP4 suitable for Bluesky upload.

    Returns the path to the transcoded file. Falls back to the original path
    if ffmpeg fails so ingest doesn't hard-crash (publish will fail instead).
    """
    out_path = input_path.rsplit(".", 1)[0] + "_transcoded.mp4"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-i", input_path,
        "-c:v", "libx264", "-c:a", "aac", "-movflags", "+faststart",
        "-y", out_path,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.warning("ffmpeg transcoding failed for %s: %s", input_path, stderr.decode()[-500:])
        return input_path
    try:
        os.remove(input_path)
    except OSError:
        pass
    return out_path


def _determine_kind(links: list[SubmissionLink], has_uploaded_image: bool, has_uploaded_video: bool = False) -> str:
    """Choose the Bluesky embed mode this submission would use."""
    first_family = links[0].domain_family if links else None
    if first_family == "bluesky" and is_bluesky_post_url(links[0].canonical_url):
        return "record"  # native repost/quote (bare profile links fall through to external)
    if has_uploaded_video:
        return "video"
    if has_uploaded_image:
        return "images"
    if links:
        return "external"
    return "empty"


def _primary_link(links: list[SubmissionLink]) -> SubmissionLink | None:
    return links[0] if links else None


def _image_status(
    kind: str, atts: list[Attachment], links: list[SubmissionLink]
) -> tuple[bool, str]:
    """Whether the at-least-one-image need is met, and where the image comes from."""
    uploaded = [a for a in atts if a.is_image]
    if kind == "record":
        return True, "n/a (Bluesky repost preserves original)"
    if kind == "video":
        videos = [a for a in atts if a.is_video]
        return True, f"{len(videos)} video(s)"
    if uploaded:
        return True, f"{len(uploaded)} uploaded image(s)"
    primary = _primary_link(links)
    if primary and primary.resolved_image_path:
        return True, f"external embed thumbnail (via {primary.resolved_via})"
    return False, "no image - post would have none"


async def _snapshot(
    session: AsyncSession, submission: Submission
) -> tuple[SubmissionSnapshot, list[Attachment], list[SubmissionLink]]:
    links = list(
        (
            await session.scalars(
                select(SubmissionLink)
                .where(SubmissionLink.submission_id == submission.id)
                .order_by(SubmissionLink.order_index)
            )
        ).all()
    )
    atts = list(
        (
            await session.scalars(
                select(Attachment).where(Attachment.submission_id == submission.id)
            )
        ).all()
    )
    has_uploaded_image = any(a.is_image for a in atts)
    has_uploaded_video = any(a.is_video for a in atts)
    kind = _determine_kind(links, has_uploaded_image, has_uploaded_video)
    primary = _primary_link(links)
    has_embed_image = bool(primary.resolved_image_path) if primary else False
    media_statuses = [AltTextStatus(a.alt_text_status) for a in atts if a.is_image or a.is_video]
    resolved_via = primary.resolved_via if primary else None
    confirmed_meta = await session.scalar(
        select(MetadataRequest).where(
            MetadataRequest.submission_id == submission.id,
            MetadataRequest.answer == "confirmed",
        )
    )
    snap = SubmissionSnapshot(
        has_canonical_link=len(links) > 0,
        image_alt_statuses=media_statuses,
        graphic_status=GraphicStatus(submission.graphic_status),
        graphic_classification_required=submission.graphic_classification_required,
        needs_image=kind in ("images", "external"),
        has_image=has_uploaded_image or has_uploaded_video or has_embed_image,
        needs_metadata=kind == "external",
        resolved_via=resolved_via,
        metadata_confirmed=confirmed_meta is not None,
        source_waived=bool(submission.source_waived),
        source_note=submission.source_note if submission.source_note_confirmed else None,
    )
    return snap, atts, links


_STATUS_TERMINAL_FOOTER = {
    SubmissionState.QUEUED.value: "queued",
    SubmissionState.PUBLISHED.value: "published to Bluesky",
    SubmissionState.PUBLISH_FAILED.value: "publish failed - will retry",
}


async def render_submission_status(session: AsyncSession, submission: Submission) -> str:
    """Render the status checklist for a submission on demand (used by /status)."""
    snap, _atts, links = await _snapshot(session, submission)
    ready = evaluate_state(snap) == SubmissionState.READY_TO_QUEUE
    source_domain = links[0].domain_family if links else None
    terminal = _STATUS_TERMINAL_FOOTER.get(submission.state)
    return replies.status_checklist(
        snap, ready=ready, source_domain=source_domain, terminal=terminal
    )


async def _has_open_request(session: AsyncSession, model, submission_id: int, **extra) -> bool:
    stmt = select(model).where(
        model.submission_id == submission_id, model.answered_at.is_(None)
    )
    for k, v in extra.items():
        stmt = stmt.where(getattr(model, k) == v)
    return (await session.scalar(stmt)) is not None


_DISCORD_MAX_BYTES = 8 * 1024 * 1024  # 8 MB free-tier upload limit


_ALT_PREVIEW_MAX_PX = 1920


def _alt_preview_for(att) -> PreviewImage | None:
    """Media preview for an alt-text prompt, or None to fall back to a URL.

    We upload the file we already downloaded so the reader sees exactly what
    they're captioning, instead of dumping the raw source URL (for resolved
    videos that URL is a long, signed, expiring CDN link - see issue #53).
    Images always upload (the helper resizes them under Discord's cap); videos
    only upload when they already fit it - a resolved video can be up to
    `_MAX_RESOLVED_VIDEO_BYTES` (Bluesky-sized), far over Discord's limit, so an
    oversized one falls back to the unfurled URL.
    """
    if not att.local_path:
        return None
    if att.is_image:
        return PreviewImage(local_path=att.local_path, filename=att.filename)
    if att.is_video:
        try:
            fits = os.path.getsize(att.local_path) <= _DISCORD_MAX_BYTES
        except OSError:
            return None
        if fits:
            return PreviewImage(local_path=att.local_path, filename=att.filename, is_video=True)
    return None


_QUEUE_TERMINAL = frozenset({
    SubmissionState.QUEUED.value,
    SubmissionState.PUBLISHED.value,
    SubmissionState.PUBLISH_FAILED.value,
})


_DEFERRED = object()  # sentinel: parent butterflied but not yet published


async def _resolve_parent_ref(session: AsyncSession, submission: Submission):
    """Resolve the Bluesky reply ref for a submission that is a Discord reply.

    Returns:
      None       - no parent, or parent not butterflied -> post standalone
      _DEFERRED  - parent butterflied but not published yet -> skip this tick
      (parent_uri, parent_cid, root_uri, root_cid) - post as Bluesky reply
    """
    if submission.reply_to_discord_message_id is None:
        return None

    parent_sub = await session.scalar(
        select(Submission).where(
            Submission.board_id == submission.board_id,
            Submission.source_discord_message_id == submission.reply_to_discord_message_id,
        )
    )
    if parent_sub is None:
        return None

    if parent_sub.state != SubmissionState.PUBLISHED.value:
        return _DEFERRED

    parent_attempt = await session.scalar(
        select(PublishAttempt)
        .where(PublishAttempt.submission_id == parent_sub.id, PublishAttempt.success.is_(True))
        .order_by(PublishAttempt.attempted_at.desc())
        .limit(1)
    )
    if parent_attempt is None or not parent_attempt.at_uri or not parent_attempt.at_cid:
        return None

    root_uri = parent_attempt.bsky_root_uri or parent_attempt.at_uri
    root_cid = parent_attempt.bsky_root_cid or parent_attempt.at_cid
    return (parent_attempt.at_uri, parent_attempt.at_cid, root_uri, root_cid)


_THREAD_CLOSE_DELAY = 15 * 60  # seconds after queuing before archiving the thread


async def _playlist_close_ready(
    session: AsyncSession,
    board_id: int,
    source_discord_message_id: int,
    board_cfg,
    playlist_skipped: bool = False,
) -> bool:
    """Return True if playlist state does not block thread archival.

    Blocks only if the auto-add hasn't been attempted yet (no DB row).
    A failed add or an opt-out both allow closure.
    """
    if not board_cfg or not board_cfg.youtube_playlist_id:
        return True
    if playlist_skipped:
        return True
    row_count = await session.scalar(
        select(func.count()).select_from(YoutubePlaylistAdd).where(
            YoutubePlaylistAdd.board_id == board_id,
            YoutubePlaylistAdd.source_discord_message_id == source_discord_message_id,
        )
    ) or 0
    return row_count > 0


def _queue_action(old_state: str, evaluated: SubmissionState) -> str:
    """Decide what to do when evaluate_state returns READY_TO_QUEUE.

    Returns one of:
      "fresh"  - first time reaching READY_TO_QUEUE; post confirmation + queue
      "silent" - was stuck at READY_TO_QUEUE; transition to QUEUED without reposting
      "none"   - already queued/published/failed; no state change
    """
    if evaluated != SubmissionState.READY_TO_QUEUE:
        return "none"
    if old_state in _QUEUE_TERMINAL:
        return "none"
    if old_state == SubmissionState.READY_TO_QUEUE.value:
        return "silent"
    return "fresh"


async def recompute_and_request(
    submission_id: int,
    *,
    settings: Settings,
    destination: Surface,
    yt_client=None,
    bot_id: int | None = None,
    from_reply: bool = False,
    ambient_session: AsyncSession | None = None,
) -> SubmissionState:
    """Re-evaluate state and post any still-missing requests (idempotently).

    Self-managing (docs/db-lock-io-refactor.md, surface-agnostic #50): a short DB
    scope reads state + open-request flags and sets the new state; then every Discord
    send happens with the lock released; then a short DB scope persists the
    request-tracking rows. Serialized per submission by ``_submission_lock`` so the
    decide->persist window stays atomic once the global lock is released around I/O.

    ``ambient_session`` is a transitional bridge for callers not yet de-scoped: pass
    the caller's live session and recompute runs entirely within it (under the caller's
    lock, sends included) instead of opening its own scopes. New/de-scoped callers pass
    nothing and must NOT hold a session_scope. ``destination`` is a ``Surface`` (callers
    wrap a raw channel at the Discord boundary before calling in).
    """
    async with _maybe_submission_lock(ambient_session, submission_id):
        # --- Decide (short DB scope): read state + open-request flags, set state. ---
        async with _scope(ambient_session) as session:
            submission = await session.get(Submission, submission_id)
            if submission is None:
                return SubmissionState.INTENT_SUBMITTED
            old_state = submission.state
            snap, atts, links = await _snapshot(session, submission)
            new_state = evaluate_state(snap)
            gaps = set(missing_gaps(snap))
            terminal = old_state in _QUEUE_TERMINAL
            # Don't overwrite state for submissions already past READY_TO_QUEUE -
            # evaluate_state is content-based and would downgrade QUEUED/PUBLISHED.
            if not terminal:
                submission.state = new_state.value
            status_message_id = submission.status_message_id

            has_cancel = await session.scalar(
                select(CancellationRequest.id).where(CancellationRequest.submission_id == submission_id)
            ) is not None
            suppl_image_open = await _has_open_request(session, SupplementalImageRequest, submission_id)
            suppl_link_open = await _has_open_request(session, SupplementalLinkRequest, submission_id)
            source_open = await _has_open_request(session, SourceRequest, submission_id)
            metadata_open = await _has_open_request(session, MetadataRequest, submission_id)
            image_open = await _has_open_request(session, ImageRequest, submission_id)
            graphic_notice = await session.scalar(
                select(ContentLabelRequest.id).where(ContentLabelRequest.submission_id == submission_id)
            ) is not None

            needed_alt_atts = []
            if Gap.ALT_TEXT in gaps:
                for att in atts:
                    if ((att.is_image or att.is_video)
                            and att.alt_text_status == AltTextStatus.NEEDED.value
                            and not await _has_open_request(
                                session, AttachmentAltTextRequest, submission_id, attachment_id=att.id)):
                        needed_alt_atts.append(att)

            ready = new_state == SubmissionState.READY_TO_QUEUE
            source_domain = links[0].domain_family if links else None
            checklist_content = replies.status_checklist(snap, ready=ready, source_domain=source_domain)

            stale_conf_id = stale_conf_msg_id = None
            if not terminal and not ready:
                stale_conf = await session.scalar(
                    select(ConfirmationRequest).where(
                        ConfirmationRequest.submission_id == submission_id,
                        ConfirmationRequest.confirmed_at.is_(None),
                    )
                )
                if stale_conf is not None:
                    stale_conf_id, stale_conf_msg_id = stale_conf.id, stale_conf.bot_message_id

            action = _queue_action(old_state, new_state)
            existing_conf_id = existing_conf_msg_id = None
            legacy_collapse = False
            preview_pages: list[str] = []
            confirmation_content = None
            confirm_components = None
            if action in ("fresh", "silent"):
                existing_conf = await session.scalar(
                    select(ConfirmationRequest).where(
                        ConfirmationRequest.submission_id == submission_id,
                        ConfirmationRequest.confirmed_at.is_(None),
                    )
                )
                if existing_conf is not None:
                    existing_conf_id = existing_conf.id
                    existing_conf_msg_id = existing_conf.bot_message_id
                    # Legacy: the ConfirmationRequest rode on the checklist message, whose
                    # button is stripped on every in-place edit. Repost a standalone one.
                    legacy_collapse = (
                        action == "silent"
                        and existing_conf.bot_message_id == submission.status_message_id
                    )
                board_cfg_conf = settings.board_for_channel(submission.channel_id)
                media_count = sum(1 for a in atts if a.is_image or a.is_video)
                confirmation_content = replies.confirmation_request(
                    bluesky_handle=board_cfg_conf.bluesky_handle if board_cfg_conf else None,
                    youtube_playlist_id=board_cfg_conf.youtube_playlist_id if board_cfg_conf else None,
                )
                confirm_components = prompts.confirm_components(submission_id, media_count=media_count)
                preview = await _build_post_preview(session, submission, atts, links)
                preview_pages = list(replies.format_post_preview(preview))

            has_media = any(a.is_image or a.is_video for a in atts)
            primary = _primary_link(links)
            metadata_url = primary.canonical_url if primary else "?"
            # Nag toward a known-good mirror off the *raw* link (canonical strips mirrors).
            metadata_mirror_tip = mirror_hint_for_url(primary.raw_url) if primary else None
            image_source_unavailable = primary is not None and primary.resolved_via == "unavailable"

        # --- I/O (lock released): post in order, collecting rows / deletes / new checklist id. ---
        to_add: list = []
        to_delete_conf_ids: list[int] = []
        new_status_id: int | None = None

        # Cancel button: posted once, before any other requests.
        if not has_cancel:
            try:
                msg = await destination.send(
                    replies.cancel_request(), components=prompts.cancel_components(submission_id)
                )
                to_add.append(CancellationRequest(
                    submission_id=submission_id, bot_message_id=msg.id, prompted_at=_now()))
            except SurfaceError as exc:
                log.warning("could not post cancel request for submission %s: %s", submission_id, exc)

        # Supplemental image offer (re-posted each answer, suppressed once terminal).
        if not terminal and not suppl_image_open:
            try:
                msg = await destination.send(replies.supplemental_image_request())
                to_add.append(SupplementalImageRequest(
                    submission_id=submission_id, bot_message_id=msg.id, prompted_at=_now()))
            except SurfaceError as exc:
                log.warning("could not post supplemental image request for submission %s: %s", submission_id, exc)

        # Supplemental link offer (only once a source link exists).
        if not terminal and snap.has_canonical_link and not suppl_link_open:
            try:
                msg = await destination.send(replies.supplemental_link_request())
                to_add.append(SupplementalLinkRequest(
                    submission_id=submission_id, bot_message_id=msg.id, prompted_at=_now()))
            except SurfaceError as exc:
                log.warning("could not post supplemental link request for submission %s: %s", submission_id, exc)

        if Gap.SOURCE in gaps and not source_open:
            # Mention the /nosource waiver only when there is media to post without a link.
            prompt = replies.source_request_with_waiver() if has_media else replies.source_request()
            try:
                msg = await destination.send(prompt)
                to_add.append(SourceRequest(submission_id=submission_id, bot_message_id=msg.id))
            except SurfaceError as exc:
                log.warning("could not post source request for submission %s: %s", submission_id, exc)

        if Gap.METADATA in gaps and not metadata_open:
            try:
                msg = await destination.send(
                    replies.metadata_request(metadata_url, mirror_tip=metadata_mirror_tip),
                    components=prompts.metadata_confirm_components(submission_id),
                )
                to_add.append(MetadataRequest(submission_id=submission_id, bot_message_id=msg.id))
            except SurfaceError as exc:
                log.warning("could not post metadata request for submission %s: %s", submission_id, exc)

        # IMAGE gap is suppressed while METADATA is open - a better link may provide an image.
        if Gap.IMAGE in gaps and Gap.METADATA not in gaps and not image_open:
            try:
                msg = await destination.send(replies.image_request(source_unavailable=image_source_unavailable))
                to_add.append(ImageRequest(submission_id=submission_id, bot_message_id=msg.id))
            except SurfaceError as exc:
                log.warning("could not post image request for submission %s: %s", submission_id, exc)

        for att in needed_alt_atts:
            try:
                preview = _alt_preview_for(att)
                if preview is not None:
                    try:
                        msg = await destination.send(
                            replies.alt_text_request(att.filename), preview=preview,
                        )
                    except Exception as exc:
                        log.warning("could not send media preview for alt text request (submission %s, att %s): %s", submission_id, att.id, exc)
                        msg = await destination.send(
                            replies.alt_text_request(att.filename) + f"\n{att.discord_url}"
                        )
                else:
                    msg = await destination.send(
                        replies.alt_text_request(att.filename) + f"\n{att.discord_url}"
                    )
                to_add.append(AttachmentAltTextRequest(
                    submission_id=submission_id, attachment_id=att.id, bot_message_id=msg.id))
            except SurfaceError as exc:
                log.warning("could not post alt text request for submission %s, att %s: %s", submission_id, att.id, exc)

        if snap.graphic_classification_required and not graphic_notice:
            try:
                msg = await destination.send(
                    replies.graphic_request(), components=prompts.graphic_components(submission_id)
                )
                to_add.append(ContentLabelRequest(submission_id=submission_id, bot_message_id=msg.id))
            except SurfaceError as exc:
                log.warning("could not post graphic request for submission %s: %s", submission_id, exc)

        # Live status checklist: edit in place, or (re)post if missing. Skipped when terminal.
        if not terminal:
            try:
                edited = (
                    status_message_id is not None
                    and await destination.edit_or_none(status_message_id, checklist_content)
                )
                if not edited:
                    msg = await destination.send(checklist_content)
                    new_status_id = msg.id
            except SurfaceError as exc:
                log.warning("could not upsert status checklist for submission %s: %s", submission_id, exc)

        # Regression out of ready: tombstone the stale Queue button and drop its row.
        if stale_conf_id is not None:
            await destination.disable_components(stale_conf_msg_id, "Not ready - see checklist")
            to_delete_conf_ids.append(stale_conf_id)

        # Fresh/silent: ensure a live confirmation button (with preview) exists.
        if action in ("fresh", "silent"):
            has_conf = existing_conf_id is not None
            if has_conf and legacy_collapse:
                log.warning(
                    "confirmation for submission %s rode on the checklist message %s "
                    "(button stripped); reposting a standalone confirmation",
                    submission_id, existing_conf_msg_id,
                )
                to_delete_conf_ids.append(existing_conf_id)
                has_conf = False
            elif has_conf and action == "silent":
                if not await destination.message_exists(existing_conf_msg_id):
                    log.warning(
                        "confirmation message %s for submission %s was deleted; reposting",
                        existing_conf_msg_id, submission_id,
                    )
                    to_delete_conf_ids.append(existing_conf_id)
                    has_conf = False
            if not has_conf:
                try:
                    for page in preview_pages:
                        await destination.send(page)
                except SurfaceError as exc:
                    log.warning("could not post preview for submission %s: %s", submission_id, exc)
                # Post the confirmation (buttons) last so it sits below the preview.
                try:
                    msg = await destination.send(confirmation_content, components=confirm_components)
                    to_add.append(ConfirmationRequest(submission_id=submission_id, bot_message_id=msg.id))
                except SurfaceError as exc:
                    log.warning("could not post confirmation request for submission %s: %s", submission_id, exc)

        # Reply that resolved the last alt-text gap on a queued submission: confirm + archive.
        if from_reply and terminal and Gap.ALT_TEXT not in gaps:
            try:
                await destination.send(replies.updated_notice())
            except SurfaceError as exc:
                log.warning("could not post updated notice for submission %s: %s", submission_id, exc)
            await destination.archive(replies.closing_notice("updated"))

        # --- Persist (short DB scope): tracking rows + checklist id + deletions. ---
        async with _scope(ambient_session) as session:
            # Delete stale confirmation rows BEFORE inserting a fresh one - the
            # confirmation_requests.submission_id unique constraint forbids two at once.
            for cid in to_delete_conf_ids:
                row = await session.get(ConfirmationRequest, cid)
                if row is not None:
                    await session.delete(row)
            await session.flush()
            for row in to_add:
                session.add(row)
            if new_status_id is not None:
                submission = await session.get(Submission, submission_id)
                if submission is not None:
                    submission.status_message_id = new_status_id

    return new_state


async def _build_post_preview(
    session: AsyncSession,
    submission: Submission,
    atts: list[Attachment],
    links: list[SubmissionLink],
) -> replies.PostPreview:
    board = await session.get(Board, submission.board_id)
    nsfw = board.nsfw if board else False
    has_uploaded_image = any(a.is_image for a in atts)
    has_uploaded_video = any(a.is_video for a in atts)
    kind = _determine_kind(links, has_uploaded_image, has_uploaded_video)
    image_satisfied, image_source = _image_status(kind, atts, links)
    primary = _primary_link(links)

    labels: list[str] = []
    if nsfw:
        labels.append("sexual")  # board-level NSFW self-label
    if submission.graphic_status == GraphicStatus.GRAPHIC.value:
        labels.append("graphic-media")

    reply_to_bsky_url: str | None = None
    reply_to_pending = False
    parent_ref = await _resolve_parent_ref(session, submission)
    if parent_ref is _DEFERRED:
        reply_to_pending = True
    elif parent_ref is not None:
        parent_sub = await session.scalar(
            select(Submission).where(
                Submission.board_id == submission.board_id,
                Submission.source_discord_message_id == submission.reply_to_discord_message_id,
            )
        )
        if parent_sub is not None:
            parent_attempt = await session.scalar(
                select(PublishAttempt)
                .where(PublishAttempt.submission_id == parent_sub.id, PublishAttempt.success.is_(True))
                .order_by(PublishAttempt.attempted_at.desc())
                .limit(1)
            )
            if parent_attempt and parent_attempt.bsky_url:
                reply_to_bsky_url = parent_attempt.bsky_url
            elif parent_attempt and parent_attempt.at_uri:
                reply_to_bsky_url = publisher.at_uri_to_url(parent_attempt.at_uri)

    return replies.PostPreview(
        kind=kind,
        title=primary.resolved_title if primary else None,
        links=[(link.canonical_url, link.domain_family, link.resolved_title) for link in links],
        images=[(a.filename, a.alt_text_body) for a in atts if a.is_image],
        videos=[(a.filename, a.alt_text_body) for a in atts if a.is_video],
        embed_title=primary.resolved_title if primary else None,
        embed_description=primary.resolved_description if primary else None,
        embed_has_thumb=bool(primary.resolved_image_path) if primary else False,
        resolved_via=primary.resolved_via if primary else None,
        labels=labels,
        board_name=board.name if board else str(submission.board_id),
        nsfw=nsfw,
        graphic_status=submission.graphic_status,
        image_satisfied=image_satisfied,
        image_source=image_source,
        reply_to_bsky_url=reply_to_bsky_url,
        reply_to_pending=reply_to_pending,
        source_note=submission.source_note if submission.source_note_confirmed else None,
    )


def _is_authorized(
    member: object | None,
    user_id: int,
    submission: Submission,
    board_cfg: BoardConfig | None,
) -> bool:
    if user_id == submission.author_id:
        return True
    if board_cfg is None:
        return False
    role_ids = {r.id for r in getattr(member, "roles", [])}
    return any(rid in role_ids for rid in board_cfg.curator_role_ids)


async def handle_reply(
    session: AsyncSession,
    event: ReplyEvent,
    surface: Surface,
    settings: Settings,
    http_client: httpx.AsyncClient,
    yt_client=None,
) -> bool:
    """If ``event`` answers one of our open requests, apply it. Returns handled?

    ``event`` carries the answered request's ``bot_message_id``, the replier
    (``author_id``/``member``), and the reply content/attachments as an
    ``InboundMessage``. All outbound nudges/notices go through ``surface``."""
    req = None
    for model in (SourceRequest, AttachmentAltTextRequest, ImageRequest, MetadataRequest, SupplementalImageRequest, SupplementalLinkRequest):
        req = await session.scalar(select(model).where(model.bot_message_id == event.bot_message_id))
        if req is not None:
            break
    if req is None:
        return False  # reply to something that isn't one of our prompts

    submission = await session.get(Submission, req.submission_id)
    if submission is None:
        return False
    board_cfg = settings.board_for_channel(submission.channel_id)
    if not _is_authorized(event.member, event.author_id, submission, board_cfg):
        return False  # silently ignore non-curators

    # Alt-text replies may overwrite a previous answer (fix a typo, rewrite); other
    # request types still ignore duplicate replies once answered.
    if req.answered_at is not None and not isinstance(req, AttachmentAltTextRequest):
        return True  # already satisfied; ignore duplicate

    handled = await _apply_answer(session, req, submission, event, surface, settings, http_client)
    if not handled:
        return True  # we replied with a nudge; leave request open

    # Replies arrive in the submission's thread, so post follow-ups right there.
    await recompute_and_request(submission.id, settings=settings, destination=surface, yt_client=yt_client, from_reply=True, ambient_session=session)
    return True


async def _apply_answer(
    session: AsyncSession,
    req,
    submission: Submission,
    event: ReplyEvent,
    surface: Surface,
    settings: Settings,
    http_client: httpx.AsyncClient,
) -> bool:
    """Apply a single reply. Returns False if the answer was unusable (nudged)."""
    if isinstance(req, (ImageRequest, SupplementalImageRequest)):
        image_atts = [
            a for a in event.message.attachments
            if is_image_attachment(a.content_type, a.filename)
            or is_video_attachment(a.content_type, a.filename)
        ]
        if not image_atts:
            await surface.send(replies.media_not_found())
            return False
        for att in image_atts:
            await _ingest_attachment_in_session(session, submission, att, settings, http_client)

    elif isinstance(req, SourceRequest):
        urls = extract_urls(event.message.content)
        if not urls:
            # No URL in the reply. It might be a genuine non-URL source (an old magazine,
            # a historical document, etc.). Stash it as a candidate note and ask for
            # confirmation, so ordinary thread chatter is never silently taken as a source.
            candidate = (event.message.content or "").strip()
            if not candidate:
                await surface.send(replies.source_not_found())
                return False
            submission.source_note = candidate[:_SOURCE_NOTE_MAX]
            submission.source_note_confirmed = False
            await surface.send(
                replies.source_note_confirm(submission.source_note),
                components=prompts.source_note_confirm_components(submission.id),
            )
            return False
        start = await session.scalar(
            select(SubmissionLink.order_index)
            .where(SubmissionLink.submission_id == submission.id)
            .order_by(SubmissionLink.order_index.desc())
        )
        next_index = (start or 0) + 1 if start is not None else 0
        for offset, raw in enumerate(urls):
            res = canonicalize(raw)
            session.add(
                SubmissionLink(
                    submission_id=submission.id,
                    order_index=next_index + offset,
                    raw_url=raw,
                    canonical_url=res.canonical_url,
                    domain_family=res.domain_family,
                )
            )
        await session.flush()  # assign link IDs before resolving
        await _resolve_links_in_session(session, submission, settings, http_client)

    elif isinstance(req, SupplementalLinkRequest):
        urls = extract_urls(event.message.content)
        if not urls:
            await surface.send(replies.supplemental_link_not_found())
            return False
        start = await session.scalar(
            select(SubmissionLink.order_index)
            .where(SubmissionLink.submission_id == submission.id)
            .order_by(SubmissionLink.order_index.desc())
        )
        next_index = (start + 1) if start is not None else 1
        for offset, raw in enumerate(urls):
            res = canonicalize(raw)
            session.add(
                SubmissionLink(
                    submission_id=submission.id,
                    order_index=next_index + offset,
                    raw_url=raw,
                    canonical_url=res.canonical_url,
                    domain_family=res.domain_family,
                )
            )
        await session.flush()
        await _resolve_links_in_session(session, submission, settings, http_client)

    elif isinstance(req, AttachmentAltTextRequest):
        body = (event.message.content or "").strip()
        if not body:
            return False
        att = await session.get(Attachment, req.attachment_id)
        if att is not None:
            # Overwriting a real previous value? Make that visible rather than silent.
            # Only flag an overwrite if the body actually changed - same value replayed
            # on boot (thread catch-up) must not re-post the notice.
            overwrote = att.alt_text_status == AltTextStatus.PROVIDED.value and att.alt_text_body != body
            previous = att.alt_text_body
            att.alt_text_body = body
            att.alt_text_status = AltTextStatus.PROVIDED.value
            att.alt_text_author = event.author_id
            if overwrote:
                await surface.send(replies.alt_text_overwritten(att.filename, previous))

    elif isinstance(req, MetadataRequest):
        urls = extract_urls(event.message.content)
        if not urls:
            await surface.send(replies.metadata_url_not_found())
            return False
        new_raw = urls[0]
        canon = canonicalize(new_raw)
        primary = _primary_link(
            list((await session.scalars(
                select(SubmissionLink)
                .where(SubmissionLink.submission_id == submission.id)
                .order_by(SubmissionLink.order_index)
            )).all())
        )
        if primary is not None:
            primary.raw_url = new_raw
            primary.canonical_url = canon.canonical_url
            primary.domain_family = canon.domain_family
            primary.resolved_title = None
            primary.resolved_description = None
            primary.resolved_image_url = None
            primary.resolved_image_path = None
            primary.resolved_via = None
        await surface.send(replies.metadata_link_updated(canon.canonical_url))
        await _resolve_links_in_session(session, submission, settings, http_client)

    req.answer = event.message.content
    req.answered_by = event.author_id
    req.answered_at = _now()
    return True
