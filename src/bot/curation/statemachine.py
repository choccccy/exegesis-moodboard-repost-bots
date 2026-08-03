"""Submission state machine + post-preview rendering: recompute readiness and post/answer requests.

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
from . import base

log = logging.getLogger(__name__)


def _gap_summary(gaps) -> str:
    """Human-readable, comma-separated list of blocking gaps for a refusal notice."""
    return ", ".join(g.value.replace("_", " ") for g in gaps)


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
    async with base._maybe_submission_lock(ambient_session, submission_id):
        # --- Decide (short DB scope): read state + open-request flags, set state. ---
        async with base._scope(ambient_session) as session:
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
                    submission_id=submission_id, bot_message_id=msg.id, prompted_at=base._now()))
            except SurfaceError as exc:
                log.warning("could not post cancel request for submission %s: %s", submission_id, exc)

        # Supplemental image offer (re-posted each answer, suppressed once terminal).
        if not terminal and not suppl_image_open:
            try:
                msg = await destination.send(replies.supplemental_image_request())
                to_add.append(SupplementalImageRequest(
                    submission_id=submission_id, bot_message_id=msg.id, prompted_at=base._now()))
            except SurfaceError as exc:
                log.warning("could not post supplemental image request for submission %s: %s", submission_id, exc)

        # Supplemental link offer (only once a source link exists).
        if not terminal and snap.has_canonical_link and not suppl_link_open:
            try:
                msg = await destination.send(replies.supplemental_link_request())
                to_add.append(SupplementalLinkRequest(
                    submission_id=submission_id, bot_message_id=msg.id, prompted_at=base._now()))
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
        async with base._scope(ambient_session) as session:
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
