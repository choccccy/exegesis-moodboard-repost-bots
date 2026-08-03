"""Surface-agnostic reaction/reply/button handlers, auth, playlist, triage, and duplicate detection.

Part of the surface-agnostic curation core (no Discord/chat SDK imports; guarded by
tests/test_curation_boundary.py). Was previously all in curation/core.py.
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
from . import base, ingest, statemachine

log = logging.getLogger(__name__)


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
    req.answered_at = base._now()
    # The reaction is on a message in the thread, so `surface` is the thread.
    await statemachine.recompute_and_request(submission.id, settings=settings, destination=surface, yt_client=yt_client, ambient_session=session)


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
    req.answered_at = base._now()
    await surface.send(replies.metadata_confirmed())
    await statemachine.recompute_and_request(submission.id, settings=settings, destination=surface, yt_client=yt_client, ambient_session=session)


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
    if submission is None or submission.state in statemachine._QUEUE_TERMINAL:
        return False
    board_cfg = settings.board_for_channel(submission.channel_id)
    if not _reaction_authorized(event.member, event.user_id, submission, board_cfg):
        return False

    # Re-validate gaps at react time: a gap (e.g. alt text for a late-added image) may have
    # opened after this confirmation was posted. Refuse and refresh rather than queue blindly.
    snap, _atts, links = await statemachine._snapshot(session, submission)
    gaps = missing_gaps(snap)
    if gaps:
        log.info("refusing to queue submission %s via ✅: gaps reopened (%s)", submission.id, statemachine._gap_summary(gaps))
        await statemachine.recompute_and_request(
            submission.id, settings=settings, destination=surface, yt_client=yt_client, ambient_session=session
        )
        await surface.send(replies.queue_blocked_notice(statemachine._gap_summary(gaps)))
        return False

    req.confirmed_at = base._now()
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
    if await statemachine._playlist_close_ready(
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
            if queued_at else statemachine._THREAD_CLOSE_DELAY
        )
        remaining = max(0.0, statemachine._THREAD_CLOSE_DELAY - elapsed)
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
    if submission is None or submission.state in statemachine._QUEUE_TERMINAL:
        return Ack("Nothing to queue.")

    board_cfg = settings.board_for_channel(submission.channel_id)
    if not _reaction_authorized(event.member, event.user_id, submission, board_cfg):
        return Ack("You're not authorised to queue this submission.")

    # Re-validate gaps at click time: a gap (e.g. alt text for a late-added image) may have
    # opened after this button was posted. Refuse and refresh rather than queue blindly.
    snap, _atts, links = await statemachine._snapshot(session, submission)
    gaps = missing_gaps(snap)
    if gaps:
        log.info("refusing to queue submission %s via button: gaps reopened (%s)", submission.id, statemachine._gap_summary(gaps))
        await statemachine.recompute_and_request(
            submission.id, settings=settings, destination=surface, yt_client=yt_client, ambient_session=session
        )
        return Ack(
            f"Can't queue yet - still needs: {statemachine._gap_summary(gaps)}. See the checklist in this thread."
        )

    req.confirmed_at = base._now()
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
    if await statemachine._playlist_close_ready(
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
    req.answered_at = base._now()

    await surface.send(replies.metadata_confirmed())
    await statemachine.recompute_and_request(submission.id, settings=settings, destination=surface, yt_client=yt_client, ambient_session=session)
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
    req.answered_at = base._now()

    await statemachine.recompute_and_request(submission.id, settings=settings, destination=surface, yt_client=yt_client, ambient_session=session)
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
        req.answered_at = base._now()
    await destination.send(replies.no_source_marked())
    await statemachine.recompute_and_request(submission.id, settings=settings, destination=destination, yt_client=yt_client, ambient_session=session)
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
        req.answered_at = base._now()
    await destination.send(replies.alt_text_skipped_all(len(pending)))
    await statemachine.recompute_and_request(submission.id, settings=settings, destination=destination, yt_client=yt_client, ambient_session=session)
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
        req.answered_at = base._now()

    await surface.send(replies.source_note_confirmed(submission.source_note))
    await statemachine.recompute_and_request(submission.id, settings=settings, destination=surface, yt_client=yt_client, ambient_session=session)
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
    await statemachine.recompute_and_request(submission.id, settings=settings, destination=surface, yt_client=yt_client, ambient_session=session)
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
            if queued_at else statemachine._THREAD_CLOSE_DELAY
        )
        remaining = max(0.0, statemachine._THREAD_CLOSE_DELAY - elapsed)
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
    await statemachine.recompute_and_request(submission.id, settings=settings, destination=surface, yt_client=yt_client, from_reply=True, ambient_session=session)
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
            await ingest._ingest_attachment_in_session(session, submission, att, settings, http_client)

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
        await ingest._resolve_links_in_session(session, submission, settings, http_client)

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
        await ingest._resolve_links_in_session(session, submission, settings, http_client)

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
        primary = statemachine._primary_link(
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
        await ingest._resolve_links_in_session(session, submission, settings, http_client)

    req.answer = event.message.content
    req.answered_by = event.author_id
    req.answered_at = base._now()
    return True
