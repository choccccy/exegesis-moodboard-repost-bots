"""DB orchestration for the ingestion loop.

Pure-ish service layer: it owns the submission lifecycle (create, ingest content,
recompute readiness, post/answer procedural requests) and talks to Discord only
to post replies and read attachments. Kept separate from the event wiring in
client.py so the logic is easy to follow and extend toward Matrix later.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone

import discord
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
from ..curation.surface import NullSurface, Surface, SurfaceError
from ..curation.types import InboundAttachment, InboundMessage
from . import render
from ..curation import prompts, replies
from ..curation.events import InteractionEvent, ReactionEvent, ReplyEvent
from ..curation.outcomes import Ack, HandlerOutcome, Noop, OpenModal, Tombstone
from .adapters import discord_message_to_inbound
from .discord_notifier import DiscordSurface
from ..curation.urls import extract_urls, is_discord_internal_url
from ..curation.components import PreviewImage
from ..db import session_scope

log = logging.getLogger(__name__)

from ..curation import base, handlers, ingest, statemachine  # noqa: E402  (agnostic core; service is the Discord layer)


# Keyed by Discord message ID. Prevents two concurrent handle_reaction calls for
# the same message from both seeing submission=None, both posting anchor pings,
# and then one failing the unique constraint after the ping is already sent.
_message_processing_locks: dict[int, asyncio.Lock] = {}


async def handle_reaction(
    *,
    settings: Settings,
    message: discord.Message,
    http_client: httpx.AsyncClient,
    member: discord.Member | None = None,
    user_id: int = 0,
    skip_auth: bool = False,
    yt_client=None,
    bot_id: int | None = None,
) -> bool:
    """Entry point for a 🦋 reaction on a watched channel message.

    Self-managing (see docs/db-lock-io-refactor.md): opens its own short DB scopes
    so thread creation and the anchor post - the rate-limited Discord calls - happen
    with the DB write lock released. The caller must NOT wrap this in a session_scope
    (a nested acquire would deadlock the non-reentrant global lock). The per-message
    lock still serializes concurrent 🦋 reactions on the same message.
    """
    async with _message_processing_locks.setdefault(message.id, asyncio.Lock()):
        # Beat 1 (DB): resolve board + authorize, load-or-create the submission and
        # ingest its content, then read the title + existing thread mapping.
        async with session_scope() as session:
            board = await handlers._board_for_channel(session, message.channel.id)
            if board is None:
                return False  # not a watched channel

            if not skip_auth:
                board_cfg = settings.board_for_channel(message.channel.id)
                if not handlers._is_curator(member, user_id, board_cfg):
                    return False

            submission = await session.scalar(
                select(Submission).where(
                    Submission.board_id == board.id,
                    Submission.source_discord_message_id == message.id,
                )
            )
            created = submission is None
            if created:
                cfg = settings.board_for_channel(message.channel.id)
                submission = Submission(
                    board_id=board.id,
                    source_discord_message_id=message.id,
                    channel_id=message.channel.id,
                    author_id=message.author.id,
                    author_display=getattr(message.author, "display_name", str(message.author)),
                    state=SubmissionState.INTENT_SUBMITTED.value,
                    graphic_classification_required=(
                        cfg.require_graphic_classification if cfg else True
                    ),
                    source_posted_at=message.created_at,
                    reply_to_discord_message_id=(
                        message.reference.message_id if message.reference else None
                    ),
                )
                session.add(submission)
                await session.flush()  # assign submission.id
                log.info("created submission %s for message %s", submission.id, message.id)

            submission_id = submission.id

        # Ingest links/media for a new submission with the lock released (HTTP +
        # downloads run outside any session_scope; self-managing).
        if created:
            await ingest.ingest_message_content(
                settings, discord_message_to_inbound(message), submission_id, http_client,
            )

        # Beats 2-3: create/resolve the Discord thread + anchor (lock released) and
        # persist the mapping. Self-managing, so it runs outside the scope above.
        thread, new_thread = await ensure_thread_persisted(
            settings, message, submission_id, post_anchor=created, bot_id=bot_id,
        )
        if thread is None:
            log.warning("could not create/resolve thread for submission %s", submission_id)
            return False

        # The thread now exists; the rest of the flow talks to it through the port.
        surface = DiscordSurface(thread)

        # Beat 4a (DB): is this new submission a duplicate? Detect + tear it down.
        dup_notice = None
        if created:
            async with session_scope() as session:
                submission = await session.get(Submission, submission_id)
                if submission is None:  # pragma: no cover - deleted between DB scopes mid-thread-create
                    return False
                dup_notice = await _detect_and_teardown_duplicate(session, settings, message, submission)

        if dup_notice is not None:
            # Beat 4b (I/O, lock released): post the notice, clear the 🦋 on the source
            # post, archive the thread. The notice send is best-effort - a failure must
            # NOT skip the trigger-clear + archive teardown. clear_trigger stays direct:
            # the orchestrator holds the source channel and builds no client-backed surface.
            try:
                await surface.send(dup_notice)
            except SurfaceError as exc:
                log.warning("could not post duplicate notice for message %s: %s", message.id, exc)
            await _clear_trigger_reaction(message.channel, message.id, settings.trigger_emoji)
            await surface.archive()
            return False

        # Beat 4c (lock released): recompute is self-managing (its sends run off the lock).
        await statemachine.recompute_and_request(
            submission_id, settings=settings, destination=surface, yt_client=yt_client, bot_id=bot_id,
        )
        return new_thread


async def _detect_and_teardown_duplicate(
    session: AsyncSession,
    settings: Settings,
    message: discord.Message,
    submission: Submission,
) -> str | None:
    """If a new submission duplicates existing content, delete it (and its files) and
    return the duplicate notice to post; else None. DB/filesystem only - the caller
    posts the notice, clears the 🦋, and archives the thread with the lock released."""
    guild_id = message.guild.id if message.guild else 0
    links = list(await session.scalars(
        select(SubmissionLink).where(SubmissionLink.submission_id == submission.id)
    ))
    for link in links:
        dup = await handlers._find_duplicate(session, link.canonical_url, submission.id, guild_id)
        if dup is None:
            continue
        kind, ref_url = dup
        if kind == "published":
            notice = replies.duplicate_posted(ref_url)
        elif kind == "queued":
            notice = replies.duplicate_queued(ref_url)
        else:
            notice = replies.duplicate_pending(ref_url)
        log.info("submission %s is a duplicate (%s); closing thread", submission.id, kind)
        board = await session.get(Board, submission.board_id)
        remove_submission_dir(settings.attachments_dir, board.id if board else 0, submission.id)
        await handlers._delete_submission_cascade(session, submission.id)
        return notice
    return None


async def _ensure_thread_io(
    settings: Settings,
    message: discord.Message,
    submission: Submission,
    *,
    content_title: str,
    anchor_title: str | None,
    mapping_thread_id: int | None,
    post_anchor: bool,
    bot_id: int | None = None,
) -> tuple[discord.Thread | None, bool]:
    """Resolve the per-submission private thread or create a new one, posting the
    anchor. **I/O only - no DB access** - so it runs with the DB lock released
    (see docs/db-lock-io-refactor.md). The caller persists submission.thread_id
    and the SubmissionThread mapping via _persist_thread_mapping afterward.

    ``submission`` is read-only here (its already-loaded attributes feed the
    anchor), so a detached instance is fine. ``mapping_thread_id`` is the thread
    id from the durable SubmissionThread mapping (survives 🦋 removal), or None.
    The anchor ping is re-posted when post_anchor=True (new submission), skipped
    when False (catchup re-scan of an already-live submission).

    Returns (thread, is_new); thread is None if creation was rate-limited/failed.
    """
    if mapping_thread_id is not None:
        existing = await _resolve_thread(message, mapping_thread_id)
        if existing is not None:
            if post_anchor:
                await _unarchive_thread(existing)
                await _post_thread_anchor(settings, message, submission, existing, content_title=anchor_title, bot_id=bot_id)
            return existing, False

    # Create a new private thread (no channel-visible "started a thread" system message).
    # 15-second timeout: discord.py's built-in retry waits for retry_after (up to 5 min)
    # which would stall the entire coroutine. Fail fast and let the periodic retry pick it up.
    try:
        async with asyncio.timeout(15):
            thread = await message.channel.create_thread(  # type: ignore[union-attr]
                name=content_title,
                type=discord.ChannelType.private_thread,
                invitable=False,
            )
    except TimeoutError:
        log.warning("thread creation timed out (rate limited) for message %s; will retry", message.id)
        return None, False
    except (discord.Forbidden, discord.HTTPException) as exc:
        log.warning("private thread creation failed for message %s: %s", message.id, exc)
        return None, False

    await _post_thread_anchor(settings, message, submission, thread, content_title=anchor_title, bot_id=bot_id)
    return thread, True


async def _persist_thread_mapping(session: AsyncSession, submission: Submission, thread_id: int) -> None:
    """Record the resolved thread id on the submission and upsert its durable
    SubmissionThread mapping (the reuse key that survives 🦋 removal). DB only."""
    submission.thread_id = thread_id
    mapping = await session.scalar(
        select(SubmissionThread).where(
            SubmissionThread.board_id == submission.board_id,
            SubmissionThread.source_discord_message_id == submission.source_discord_message_id,
        )
    )
    if mapping is None:
        session.add(
            SubmissionThread(
                board_id=submission.board_id,
                source_discord_message_id=submission.source_discord_message_id,
                thread_id=thread_id,
            )
        )
    else:
        mapping.thread_id = thread_id  # old thread was gone; remember the new one


async def ensure_thread_persisted(
    settings: Settings,
    message: discord.Message,
    submission_id: int,
    *,
    post_anchor: bool,
    bot_id: int | None = None,
) -> tuple[discord.Thread | None, bool]:
    """Ensure the submission's Discord thread exists and its mapping is stored,
    keeping the rate-limited thread creation + anchor out of the DB lock.

    Self-managing (opens its own short scopes): a DB read for the title + existing
    mapping, then thread creation/resolution + anchor with the lock released, then
    a DB write to persist the mapping. Callers must NOT hold a session_scope.
    Returns (thread, is_new); thread is None if the submission vanished or thread
    creation was rate-limited/failed.
    """
    # Beat 1 (DB): read the thread title and the existing mapping.
    async with session_scope() as session:
        submission = await session.get(Submission, submission_id)
        if submission is None:
            return None, False
        inbound = discord_message_to_inbound(message)
        content_title = await _derive_thread_title(session, inbound, submission)
        # Strip fallback sentinel ("🦋 submission N") - title=None tells the anchor
        # to omit the 📌 line rather than show the generic placeholder.
        anchor_title = content_title if not content_title.startswith("🦋 submission") else None
        mapping = await session.scalar(
            select(SubmissionThread).where(
                SubmissionThread.board_id == submission.board_id,
                SubmissionThread.source_discord_message_id == submission.source_discord_message_id,
            )
        )
        mapping_thread_id = mapping.thread_id if mapping is not None else None
        # submission is detached after this scope; _ensure_thread_io only reads its
        # already-loaded attributes for the anchor, which is safe.

    # Beat 2 (I/O, lock released): create/resolve the thread and post the anchor.
    thread, new_thread = await _ensure_thread_io(
        settings, message, submission,
        content_title=content_title, anchor_title=anchor_title,
        mapping_thread_id=mapping_thread_id, post_anchor=post_anchor, bot_id=bot_id,
    )
    if thread is None:
        return None, False

    # Beat 3 (DB): persist the thread id + mapping.
    async with session_scope() as session:
        submission = await session.get(Submission, submission_id)
        if submission is None:  # pragma: no cover - deleted between DB scopes mid-thread-create
            log.warning("submission %s vanished during thread creation", submission_id)
            return None, False
        await _persist_thread_mapping(session, submission, thread.id)
    return thread, new_thread


async def _post_thread_anchor(
    settings: Settings,
    message: discord.Message,
    submission: Submission,
    thread: discord.Thread,
    content_title: str | None = None,
    bot_id: int | None = None,
) -> None:
    """Anchor the private thread: ping OP, forward the source message."""
    cfg = settings.board_for_channel(submission.channel_id)

    board_display = None
    bluesky_handle = None
    youtube_playlist_id = None
    if cfg:
        board_display = cfg.display_name or cfg.name.replace("-", " ").title()
        bluesky_handle = cfg.bluesky_handle
        youtube_playlist_id = cfg.youtube_playlist_id

    bot_mention = f"<@{bot_id}>" if bot_id else "The bot"

    text = replies.thread_anchor(
        author_mention=f"<@{submission.author_id}>",
        bot_mention=bot_mention,
        board_display_name=board_display,
        bluesky_handle=bluesky_handle,
        youtube_playlist_id=youtube_playlist_id,
        content_title=content_title,
        dashboard_url=settings.dashboard_url,
    )
    try:
        await thread.send(text, allowed_mentions=discord.AllowedMentions(users=True))
    except (discord.Forbidden, discord.HTTPException) as exc:
        log.warning("could not post thread anchor for submission %s: %s", submission.id, exc)

    # Forward the original message so curators see the content inline.
    try:
        await message.forward(thread)
    except (discord.Forbidden, discord.HTTPException, AttributeError) as exc:
        # Fall back to a jump link if forward is unavailable or fails.
        guild_id = message.guild.id if message.guild else 0
        jump = (
            f"https://discord.com/channels/{guild_id}/{submission.channel_id}/"
            f"{submission.source_discord_message_id}"
        )
        log.warning("message forward failed for submission %s, falling back to jump link: %s", submission.id, exc)
        try:
            await thread.send(f"↗ {jump}")
        except (discord.Forbidden, discord.HTTPException):
            pass

    # For playlist-enabled boards, post the opt-out prompt and seed the reaction.
    if cfg and cfg.youtube_playlist_id:
        try:
            opt_msg = await thread.send(
                replies.playlist_opt_out_prompt(),
                view=render.render_components(prompts.playlist_skip_components(submission.id)),
            )
            submission.playlist_opt_out_message_id = opt_msg.id
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("could not post playlist opt-out for submission %s: %s", submission.id, exc)


async def _derive_thread_title(
    session: AsyncSession, message: InboundMessage, submission: Submission
) -> str:
    """Name the thread after the resolved post title.

    Prefers the title our resolver produced (oembed/opengraph/etc.; resolution
    runs before the thread is created), then the embed title or author name,
    then a generic fallback.
    """
    primary = await session.scalar(
        select(SubmissionLink)
        .where(SubmissionLink.submission_id == submission.id)
        .order_by(SubmissionLink.order_index)
        .limit(1)
    )
    candidates: list[str | None] = [primary.resolved_title if primary else None]
    for embed in message.embeds:
        candidates.append(embed.title or embed.author_name)
    for candidate in candidates:
        if candidate and candidate.strip():
            title = candidate.strip()
            return title if len(title) <= 100 else title[:99] + "…"
    return replies.thread_name(submission.id)


async def _clear_trigger_reaction(channel: discord.abc.Messageable, message_id: int, trigger_emoji: str) -> None:
    try:
        msg = await channel.fetch_message(message_id)  # type: ignore[union-attr]
        await msg.clear_reaction(trigger_emoji)
    except discord.Forbidden:
        log.debug("no Manage Messages permission to clear trigger reaction from message %s", message_id)
    except (discord.NotFound, discord.HTTPException) as exc:
        log.debug("could not clear trigger reaction from message %s: %s", message_id, exc)


async def _resolve_thread(
    message: discord.Message, thread_id: int
) -> discord.Thread | None:
    guild = message.guild
    if guild is None:
        return None
    cached = guild.get_thread(thread_id)
    if cached is not None:
        return cached
    try:
        channel = await guild.fetch_channel(thread_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None
    return channel if isinstance(channel, discord.Thread) else None


async def reingest_submission(
    submission_id: int,
    *,
    message: discord.Message,
    settings: Settings,
    http_client: httpx.AsyncClient,
) -> None:
    """Re-read the original Discord message and refresh this submission in place.

    Rebuilds links, embed capture, and Discord attachments from the *current* message
    state (picking up edits: changed caption, added/removed attachments, changed links)
    and re-resolves link metadata/media - the useful half of unbutterfly+rebutterfly,
    without the destructive full delete.

    Self-managing (see docs/db-lock-io-refactor.md): the tear-down and re-apply run in
    short DB scopes; the re-ingest (HTTP + downloads) goes through
    ingest_message_content with the lock released. Callers must NOT hold a session_scope.

    Preserved: curator-entered alt text (matched by the stable discord_attachment_id;
    PROVIDED and SKIPPED are deliberate resolutions) and all submission-level decisions
    (source waiver/note, graphic label, playlist opt-out) which live on the Submission
    row and are never touched here. The resolver-sourced video (discord_attachment_id
    == 0), if any, is kept as-is - ingest's has_existing_video guard makes the re-resolve
    a no-op, so the file and its alt survive (a changed upstream video won't refresh via
    reingest; rebutterfly for that).

    Non-attachment request rows (source, metadata, graphic, ...) are left intact, so
    recompute_and_request won't re-prompt for things already answered. The caller runs
    recompute_and_request afterward.
    """
    # Beat 1 (DB): capture preserved alt, tear down message-derived rows/embed.
    async with session_scope() as session:
        submission = await session.get(Submission, submission_id)
        if submission is None:
            return
        existing_atts = list(
            await session.scalars(
                select(Attachment).where(Attachment.submission_id == submission_id)
            )
        )
        # discord_attachment_id -> (body, status, author) for human-resolved alt only.
        preserved_alt = {
            a.discord_attachment_id: (a.alt_text_body, a.alt_text_status, a.alt_text_author)
            for a in existing_atts
            if a.alt_text_status in (AltTextStatus.PROVIDED.value, AltTextStatus.SKIPPED.value)
        }

        # Drop Discord-sourced attachments (rebuilt from the message) plus their per-image
        # alt-text request rows (keyed by attachment_id - they'd dangle otherwise). Keep the
        # resolver video (id 0) and its request row.
        removed_ids = [a.id for a in existing_atts if a.discord_attachment_id != 0]
        if removed_ids:
            await session.execute(
                delete(AttachmentAltTextRequest).where(
                    AttachmentAltTextRequest.attachment_id.in_(removed_ids)
                )
            )
            await session.execute(delete(Attachment).where(Attachment.id.in_(removed_ids)))

        # Links and captured embed fields are fully derived from the message - rebuild them.
        await session.execute(
            delete(SubmissionLink).where(SubmissionLink.submission_id == submission_id)
        )
        submission.embed_title = None
        submission.embed_description = None
        submission.embed_thumb_url = None

    # Beat 2: re-ingest with the lock released (self-managing).
    await ingest.ingest_message_content(
        settings, discord_message_to_inbound(message), submission_id, http_client,
    )

    # Beat 3 (DB): re-apply preserved alt onto re-created attachments matching by id.
    if preserved_alt:
        async with session_scope() as session:
            for a in await session.scalars(
                select(Attachment).where(Attachment.submission_id == submission_id)
            ):
                snap = preserved_alt.get(a.discord_attachment_id)
                if snap is not None:
                    a.alt_text_body, a.alt_text_status, a.alt_text_author = snap


def _discord_file_for_animated_gif(img: object, filename: str) -> discord.File:
    """Convert an open animated GIF to animated WebP for Discord upload.

    Tries successively lower WebP quality settings to fit within 8 MB.
    Falls back to a static first-frame JPEG if nothing fits.
    """
    from PIL import Image, ImageSequence

    w, h = img.size
    scale = min(1.0, statemachine._ALT_PREVIEW_MAX_PX / max(w, h))
    new_w, new_h = max(1, int(w * scale)), max(1, int(h * scale))

    frames: list = []
    durations: list = []
    for frame in ImageSequence.Iterator(img):
        rgba = frame.convert("RGBA")
        if scale < 1.0:
            rgba = rgba.resize((new_w, new_h), Image.LANCZOS)
        frames.append(rgba)
        durations.append(frame.info.get("duration", 100))

    stem = filename.rsplit(".", 1)[0] if "." in filename else filename

    for quality in (80, 60, 40):
        buf = io.BytesIO()
        frames[0].save(
            buf, format="WEBP", save_all=True, append_images=frames[1:],
            duration=durations, loop=0, quality=quality,
        )
        buf.seek(0)
        if buf.getbuffer().nbytes <= statemachine._DISCORD_MAX_BYTES:
            return discord.File(buf, filename=f"{stem}.webp")

    # Nothing fit as animated WebP - show first frame only.
    buf = io.BytesIO()
    frames[0].convert("RGB").save(buf, format="JPEG", quality=85)
    buf.seek(0)
    return discord.File(buf, filename=f"{stem}.jpg")


def _discord_file_for_attachment(local_path: str, filename: str) -> discord.File:
    """Return a discord.File for the image, resizing in-memory if it exceeds 8 MB."""
    from PIL import Image

    with Image.open(local_path) as img:
        # Capture format before resize - img.resize() returns a new object with format=None.
        fmt = img.format or "JPEG"
        if fmt not in ("JPEG", "PNG", "WEBP", "GIF"):
            fmt = "JPEG"

        # Animated GIFs need per-frame processing; delegate to a dedicated helper.
        if fmt == "GIF" and getattr(img, "n_frames", 1) > 1:
            return _discord_file_for_animated_gif(img, filename)

        # JPEG can't store alpha; flatten RGBA to RGB before encoding.
        if fmt == "JPEG" and img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGB")
        w, h = img.size
        if max(w, h) > statemachine._ALT_PREVIEW_MAX_PX:
            scale = statemachine._ALT_PREVIEW_MAX_PX / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format=fmt)
        buf.seek(0)
        if buf.getbuffer().nbytes > statemachine._DISCORD_MAX_BYTES:
            # Still too large after resize - re-encode as JPEG at reduced quality
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=70)
            buf.seek(0)
        return discord.File(buf, filename=filename)


async def cancel_submission_for_deleted_thread(
    session: AsyncSession, settings: Settings, thread_id: int
) -> int | None:
    """A submission's Discord thread was deleted - purge the orphaned open submission so
    it stops cluttering triage with a dead link (it can never be actioned without a thread).

    Terminal submissions (queued/published/failed) are left intact: they don't need a live
    thread and stand as a record. Returns the purged submission id, or None if nothing was.
    """
    submission = await session.scalar(
        select(Submission).where(Submission.thread_id == thread_id)
    )
    if submission is None or submission.state in statemachine._QUEUE_TERMINAL:
        return None
    sub_id = submission.id
    board = await session.get(Board, submission.board_id)
    remove_submission_dir(settings.attachments_dir, board.id if board else 0, sub_id)
    await handlers._delete_submission_cascade(session, sub_id)
    log.info("purged submission %s: its Discord thread %s was deleted", sub_id, thread_id)
    return sub_id


# Strong references to fire-and-forget tasks - prevents GC from cancelling them
# before the sleep completes (asyncio footgun: bare create_task result is weakly held).
_background_tasks: set[asyncio.Task] = set()


def _fire_and_forget(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _archive_thread_after_delay_seconds(
    thread: discord.Thread, delay: float, *, notice: str | None = None
) -> None:
    """Archive (close) a private thread after `delay` seconds.

    Archiving removes it from members' sidebars without deleting any content.
    The bot can still unarchive it later to post the publish confirmation.
    Runs as a fire-and-forget background task.
    """
    if delay > 0:
        await asyncio.sleep(delay)
    if notice:
        try:
            await thread.send(notice)
        except Exception:
            pass
    try:
        await thread.edit(archived=True)
        log.debug("archived thread %s", thread.id)
    except Exception:
        log.warning("failed to archive thread %s", thread.id, exc_info=True)


def _archive_thread_after_delay(thread: discord.Thread, *, notice: str | None = None) -> None:
    """Schedule archival of a thread after the standard close delay."""
    _fire_and_forget(_archive_thread_after_delay_seconds(thread, statemachine._THREAD_CLOSE_DELAY, notice=notice))


async def _archive_thread(thread: discord.Thread, *, notice: str | None = None) -> None:
    """Immediately archive (close) a thread.

    NB: we do NOT gate on ``thread.archived``. discord.py's ``Thread.edit()`` returns a
    fresh object rather than mutating in place, so a thread that was unarchived earlier in
    the same flow (e.g. reused then re-closed) still reports ``archived=True`` locally.
    Trusting that stale flag silently skipped the archive - the notice would post and the
    thread would stay open. Issuing the edit unconditionally is a harmless no-op when the
    thread really is archived.
    """
    if notice:
        try:
            await thread.send(notice)
        except Exception:
            pass
    try:
        await thread.edit(archived=True)
        log.debug("archived thread %s", thread.id)
    except Exception:
        log.warning("failed to archive thread %s", thread.id, exc_info=True)


async def _unarchive_thread(thread: discord.Thread) -> None:
    """Reopen an archived thread so the bot can post into it.

    Like _archive_thread, this does not trust the possibly-stale local ``archived`` flag
    (Thread.edit() returns a new object, never mutating in place) - it always issues the
    edit, which is a no-op when the thread is already open.
    """
    try:
        await thread.edit(archived=False)
        log.debug("unarchived thread %s for reuse", thread.id)
    except Exception:
        log.warning("failed to unarchive thread %s", thread.id, exc_info=True)


@dataclass
class _PublishPlan:
    """Beat-1 decision: everything the network publish needs, captured while the
    DB lock is held so the lock can be released before any I/O. The ORM objects
    are detached after the session closes; publish_submission only reads their
    (already-loaded) column attributes, which is safe with expire_on_commit=False."""
    submission: Submission
    links: list[SubmissionLink]
    atts: list[Attachment]
    board_cfg: BoardConfig
    password: str
    reply_kwargs: dict
    curator_user_ids: list[int] | None


async def _safe_send(destination: Surface, content: str, what: str, submission_id: int) -> None:
    """Send a Discord notice, swallowing failures (a publish must never be rolled
    back or a queue tick wasted because a status message couldn't be delivered)."""
    try:
        await destination.send(content)
    except Exception as exc:
        log.warning("submission %s: could not send %s: %s", submission_id, what, exc)


async def _find_publish_time_duplicate(
    session: AsyncSession, submission: Submission, links: list[SubmissionLink]
):
    """Return a prior successful PublishAttempt whose canonical_url matches one of
    this submission's links (a duplicate that slipped into the queue), or None."""
    for link in links:
        if link.canonical_url is None:
            continue  # null canonical_url would match all other nulls; skip the check
        prior_attempt = await session.scalar(
            select(PublishAttempt)
            .join(Submission, PublishAttempt.submission_id == Submission.id)
            .join(SubmissionLink, SubmissionLink.submission_id == Submission.id)
            .where(
                SubmissionLink.canonical_url == link.canonical_url,
                PublishAttempt.success.is_(True),
                PublishAttempt.error.is_(None),  # only real publishes, not cascaded suppression rows
                Submission.id != submission.id,
            )
            .order_by(PublishAttempt.id.desc())
            .limit(1)
        )
        if prior_attempt is not None:
            return prior_attempt
    return None


async def publish_queued_submission(
    settings: Settings,
    submission_id: int,
    destination: Surface | None = None,
) -> PublishOutcome:
    """Publish a QUEUED or PUBLISH_FAILED submission, keeping all network and
    Discord I/O out of the DB write lock (see docs/db-lock-io-refactor.md).

    Runs in beats: a short DB transaction to load state and decide (beat 1), the
    Bluesky network publish with no lock held (beat 2), a short DB transaction to
    record the result (beat 3), then the Discord status notice (beat 4). Opens its
    own sessions - the caller must NOT hold a session_scope, or the beat-1 acquire
    would deadlock on the (non-reentrant) global lock.

    ``destination`` is the submission thread (or None if the thread can't be
    resolved - publish still proceeds, just without a Discord status notice).
    Returns a PublishOutcome so the scheduler can decide whether the tick is spent.
    """
    if destination is None:
        destination = NullSurface()

    # ---- Beat 1: load, validate, decide (short DB scope; no I/O) ----
    plan: _PublishPlan | None = None
    async with session_scope() as session:
        submission = await session.get(Submission, submission_id)
        if submission is None:
            log.warning("publish: submission %s vanished before publish", submission_id)
            return PublishOutcome.FAILED
        _snap, atts, links = await statemachine._snapshot(session, submission)
        board_cfg = settings.board_for_channel(submission.channel_id)

        if not board_cfg or not board_cfg.bluesky_handle:
            err = "board has no Bluesky handle configured"
            log.warning("submission %s: %s", submission_id, err)
            submission.state = SubmissionState.PUBLISH_FAILED.value
            session.add(PublishAttempt(submission_id=submission_id, success=False, error=err))
            mention = board_cfg.curator_user_ids if board_cfg else None
            early: tuple = ("FAILED", err, mention)
        elif not (password := settings.bsky_password_for(board_cfg.name)):
            err = f"no app password configured for board {board_cfg.name}"
            log.warning("submission %s: %s", submission_id, err)
            submission.state = SubmissionState.PUBLISH_FAILED.value
            session.add(PublishAttempt(submission_id=submission_id, success=False, error=err))
            early = ("FAILED", err, board_cfg.curator_user_ids)
        else:
            parent_ref = await statemachine._resolve_parent_ref(session, submission)
            if parent_ref is statemachine._DEFERRED:
                early = ("DEFERRED",)
            elif (dup := await _find_publish_time_duplicate(session, submission, links)) is not None:
                bsky_url = dup.bsky_url or dup.at_uri
                log.warning("submission %s skipped at publish time - duplicate of %s", submission_id, bsky_url)
                # Mark as PUBLISHED (not PUBLISH_FAILED) so it is not retried.
                submission.state = SubmissionState.PUBLISHED.value
                session.add(PublishAttempt(
                    submission_id=submission_id,
                    success=True,
                    at_uri=dup.at_uri,
                    at_cid=dup.at_cid,
                    bsky_root_uri=dup.bsky_root_uri,
                    bsky_root_cid=dup.bsky_root_cid,
                    bsky_url=dup.bsky_url,
                    error="duplicate: content already published by another submission",
                ))
                early = ("DUPLICATE", bsky_url)
            else:
                reply_kwargs: dict = {}
                if parent_ref is not None:
                    parent_uri, parent_cid, root_uri, root_cid = parent_ref
                    reply_kwargs = dict(
                        reply_parent_uri=parent_uri,
                        reply_parent_cid=parent_cid,
                        reply_root_uri=root_uri,
                        reply_root_cid=root_cid,
                    )
                plan = _PublishPlan(
                    submission=submission,
                    links=links,
                    atts=atts,
                    board_cfg=board_cfg,
                    password=password,
                    reply_kwargs=reply_kwargs,
                    curator_user_ids=board_cfg.curator_user_ids,
                )
                early = ()

    # ---- Beat 1 early exits: perform their I/O with the lock released ----
    if plan is None:
        if early[0] == "FAILED":
            _, err, mention = early
            await _safe_send(destination, replies.publish_failed_notice(err, mention_user_ids=mention),
                             "publish-failed notice", submission_id)
            return PublishOutcome.FAILED
        if early[0] == "DEFERRED":
            log.info("submission %s deferred: parent not yet published", submission_id)
            return PublishOutcome.DEFERRED
        # DUPLICATE
        await _safe_send(destination, replies.duplicate_posted(early[1]), "duplicate notice", submission_id)
        destination.archive_after_delay(replies.closing_notice("duplicate"))
        return PublishOutcome.DUPLICATE

    # ---- Beat 2: network publish (NO lock held) ----
    result = await publisher.publish_submission(
        submission=plan.submission,
        links=plan.links,
        attachments=plan.atts,
        board_cfg=plan.board_cfg,
        password=plan.password,
        **plan.reply_kwargs,
    )

    # ---- Beat 3: record the result (short DB scope; no I/O) ----
    published = bool(result.success and result.at_uri)
    async with session_scope() as session:
        session.add(PublishAttempt(
            submission_id=submission_id,
            success=result.success,
            at_uri=result.at_uri,
            at_cid=result.at_cid,
            bsky_root_uri=result.bsky_root_uri,
            bsky_root_cid=result.bsky_root_cid,
            bsky_url=result.bsky_url,
            error=result.error,
        ))
        submission = await session.get(Submission, submission_id)
        if submission is not None:
            submission.state = (
                SubmissionState.PUBLISHED.value if published else SubmissionState.PUBLISH_FAILED.value
            )

    # ---- Beat 4: Discord status notice (lock released) ----
    if published:
        bsky_url = result.bsky_url or publisher.at_uri_to_url(result.at_uri)
        log.info("submission %s published: %s", submission_id, result.at_uri)
        notice = replies.reposted_notice(bsky_url) if result.is_repost else replies.published_notice(bsky_url)
        await _safe_send(destination, notice, "published notice", submission_id)
        # Archive (delayed) unconditionally - even if the notice send failed, close the thread.
        destination.archive_after_delay(replies.closing_notice("published to Bluesky"))
        return PublishOutcome.PUBLISHED

    log.error("submission %s publish failed: %s", submission_id, result.error)
    await _safe_send(destination, replies.publish_failed_notice(
        result.error, mention_user_ids=plan.curator_user_ids), "publish-failed notice", submission_id)
    return PublishOutcome.FAILED
