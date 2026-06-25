"""DB orchestration for the ingestion loop.

Pure-ish service layer: it owns the submission lifecycle (create, ingest content,
recompute readiness, post/answer procedural requests) and talks to Discord only
to post replies and read attachments. Kept separate from the event wiring in
client.py so the logic is easy to follow and extend toward Matrix later.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
from datetime import datetime, timezone

import discord
import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..accessibility import initial_alt_text, is_image_attachment, is_video_attachment
from ..asset_store import (
    StorageFullError,
    download_attachment,
    remove_submission_dir,
    submission_dir,
)
from ..canonicalize import canonicalize
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
from ..resolve import resolve
from ..state import (
    AltTextStatus,
    GraphicStatus,
    Gap,
    SubmissionSnapshot,
    SubmissionState,
    evaluate_state,
    missing_gaps,
)
from . import replies
from .urls import extract_urls

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- board sync -------------------------------------------------------------


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


# --- submission creation + content ingest -----------------------------------


async def handle_reaction(
    session: AsyncSession,
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
    """Entry point for a 🦋 reaction on a watched channel message."""
    board = await _board_for_channel(session, message.channel.id)
    if board is None:
        return  # not a watched channel

    if not skip_auth:
        board_cfg = settings.board_for_channel(message.channel.id)
        if not _is_curator(member, user_id, board_cfg):
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
        )
        session.add(submission)
        await session.flush()  # assign submission.id
        await _ingest_content(session, submission, message, settings, http_client)
        await _resolve_links(session, submission, settings, http_client)
        log.info("created submission %s for message %s", submission.id, message.id)

    thread, new_thread = await _ensure_thread(session, settings, message, submission, post_anchor=created, bot_id=bot_id)
    if thread is None:
        log.warning("could not create/resolve thread for submission %s", submission.id)
        return False

    if created:
        links = list(await session.scalars(
            select(SubmissionLink).where(SubmissionLink.submission_id == submission.id)
        ))
        for link in links:
            prior = await _find_prior_post(session, link.canonical_url, submission.id)
            if prior:
                await thread.send(replies.duplicate_warning(prior))
                break

    await recompute_and_request(session, submission, settings=settings, destination=thread, yt_client=yt_client, bot_id=bot_id)
    return new_thread


async def _ensure_thread(
    session: AsyncSession,
    settings: Settings,
    message: discord.Message,
    submission: Submission,
    post_anchor: bool = True,
    bot_id: int | None = None,
) -> tuple[discord.Thread | None, bool]:
    """Get (or create) the per-submission *private* thread.

    Reuse is keyed by a durable SubmissionThread mapping (survives 🦋 removal).
    The anchor ping is re-posted when post_anchor=True (new submission), skipped
    when False (catchup re-scan of an already-live submission).
    """
    # Derive the content title once - used for both the thread name and the anchor message.
    content_title = await _derive_thread_title(session, message, submission)
    # Strip fallback sentinel ("🦋 submission N") before passing to anchor - title=None
    # tells the anchor to omit the 📌 line rather than show the generic placeholder.
    anchor_title: str | None = content_title if not content_title.startswith("🦋 submission") else None

    mapping = await session.scalar(
        select(SubmissionThread).where(
            SubmissionThread.board_id == submission.board_id,
            SubmissionThread.source_discord_message_id == submission.source_discord_message_id,
        )
    )
    if mapping is not None:
        existing = await _resolve_thread(message, mapping.thread_id)
        if existing is not None:
            submission.thread_id = mapping.thread_id
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

    submission.thread_id = thread.id
    if mapping is None:
        session.add(
            SubmissionThread(
                board_id=submission.board_id,
                source_discord_message_id=submission.source_discord_message_id,
                thread_id=thread.id,
            )
        )
    else:
        mapping.thread_id = thread.id  # old thread was gone; remember the new one
    return thread, True


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
            opt_msg = await thread.send(replies.playlist_opt_out_prompt())
            await opt_msg.add_reaction(replies.PLAYLIST_OPT_OUT_EMOJI)
            submission.playlist_opt_out_message_id = opt_msg.id
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("could not post playlist opt-out for submission %s: %s", submission.id, exc)


async def _derive_thread_title(
    session: AsyncSession, message: discord.Message, submission: Submission
) -> str:
    """Name the thread after the resolved post title.

    Prefers the title our resolver produced (oembed/opengraph/etc.; resolution
    runs before the thread is created), then the Discord-generated embed title,
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
        candidates.append(embed.title or (embed.author.name if embed.author else None))
    for candidate in candidates:
        if candidate and candidate.strip():
            title = candidate.strip()
            # Discord caps thread names at 100 chars.
            return title if len(title) <= 100 else title[:99] + "…"
    return replies.thread_name(submission.id)


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


async def handle_reaction_removed(
    session: AsyncSession,
    *,
    settings: Settings,
    channel: discord.abc.Messageable,
    channel_id: int,
    message_id: int,
    user_id: int,
) -> None:
    """A 🦋 was removed: delete the prospective post so a re-react starts fresh.

    Only curators (by role or explicit user ID) may trigger deletion this way.
    The OP can cancel via the ❌ button in the thread instead.

    Deletes the submission, its links/attachments/requests, and the downloaded
    files, then posts a short notice. Re-adding 🦋 re-runs ingest via
    handle_reaction (get-or-create will create a new submission).
    """
    board = await _board_for_channel(session, channel_id)
    if board is None:
        return
    board_cfg = settings.board_for_channel(channel_id)
    if not await _curator_authorized(channel, user_id, board_cfg):
        return  # only curators can cancel via butterfly removal; OP uses the ❌ button
    submission = await session.scalar(
        select(Submission).where(
            Submission.board_id == board.id,
            Submission.source_discord_message_id == message_id,
        )
    )
    if submission is None:
        return  # nothing to undo

    # Block removal of already-published submissions to prevent duplicate posts.
    if submission.state == SubmissionState.PUBLISHED.value:
        thread = await _resolve_thread_by_id(channel, submission.thread_id) if submission.thread_id else None
        if thread is not None:
            attempt = await session.scalar(
                select(PublishAttempt)
                .where(PublishAttempt.submission_id == submission.id, PublishAttempt.success.is_(True))
                .order_by(PublishAttempt.attempted_at.desc())
            )
            board_cfg = settings.board_for_channel(channel_id)
            if attempt and attempt.bsky_url:
                bsky_url = attempt.bsky_url
            elif attempt and attempt.at_uri:
                bsky_url = publisher.at_uri_to_url(attempt.at_uri, board_cfg.bluesky_handle if board_cfg else None)
            else:
                bsky_url = "Bluesky"
            await thread.send(replies.cannot_remove_published(bsky_url))
        return

    sub_id = submission.id
    thread_id = submission.thread_id
    remove_submission_dir(settings.attachments_dir, board.id, sub_id)
    # Delete child rows, then the submission itself.
    # (SQLite has no ON DELETE CASCADE here, so delete children explicitly.)
    for model in (
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
    ):
        await session.execute(delete(model).where(model.submission_id == sub_id))
    await session.execute(delete(Submission).where(Submission.id == sub_id))
    log.info("deleted submission %s after 🦋 removal on message %s", sub_id, message_id)

    # Notice goes in the thread (never the main channel). The thread is kept and
    # reused if the 🦋 is re-added, so we don't spam the channel with new threads.
    thread = await _resolve_thread_by_id(channel, thread_id) if thread_id else None
    if thread is not None:
        await thread.send(replies.reaction_removed())
        await _archive_thread(thread, notice=replies.closing_notice("submission removed"))
    else:
        log.info("no thread to notify for removed submission %s", sub_id)


async def handle_label_reaction(
    session: AsyncSession,
    *,
    settings: Settings,
    channel: discord.abc.Messageable,
    message_id: int,
    emoji: str,
    member: discord.Member | None,
    user_id: int,
    yt_client=None,
) -> None:
    """A curator reacted ✅/❌ on a graphic-classification request message."""
    req = await session.scalar(
        select(ContentLabelRequest).where(ContentLabelRequest.bot_message_id == message_id)
    )
    if req is None or req.answered_at is not None:
        return
    status = graphic_from_emoji(emoji)
    if status is None:
        return

    submission = await session.get(Submission, req.submission_id)
    if submission is None:
        return
    board_cfg = settings.board_for_channel(submission.channel_id)
    if not _reaction_authorized(member, user_id, submission, board_cfg):
        return

    submission.graphic_status = status.value
    req.answer = emoji
    req.answered_by = user_id
    req.answered_at = _now()
    # The reaction is on a message in the thread, so `channel` is the thread.
    await recompute_and_request(session, submission, settings=settings, destination=channel, yt_client=yt_client)


async def handle_metadata_reaction(
    session: AsyncSession,
    *,
    settings: Settings,
    channel: discord.abc.Messageable,
    message_id: int,
    member: discord.Member | None,
    user_id: int,
    yt_client=None,
) -> None:
    """A curator reacted 🔗 on a metadata-request message - confirm this is the best link."""
    req = await session.scalar(
        select(MetadataRequest).where(
            MetadataRequest.bot_message_id == message_id,
            MetadataRequest.answered_at.is_(None),
        )
    )
    if req is None:
        return
    submission = await session.get(Submission, req.submission_id)
    if submission is None:
        return
    board_cfg = settings.board_for_channel(submission.channel_id)
    if not _reaction_authorized(member, user_id, submission, board_cfg):
        return

    req.answer = "confirmed"
    req.answered_by = user_id
    req.answered_at = _now()
    await channel.send(replies.metadata_confirmed())
    await recompute_and_request(session, submission, settings=settings, destination=channel, yt_client=yt_client)


async def handle_confirmation_reaction(
    session: AsyncSession,
    *,
    settings: Settings,
    channel: discord.abc.Messageable,
    message_id: int,
    member: discord.Member | None,
    user_id: int,
    yt_client=None,
) -> bool:
    """A curator or OP reacted ✅ on the confirmation prompt — queue the submission."""
    req = await session.scalar(
        select(ConfirmationRequest).where(
            ConfirmationRequest.bot_message_id == message_id,
            ConfirmationRequest.confirmed_at.is_(None),
        )
    )
    if req is None:
        return False
    submission = await session.get(Submission, req.submission_id)
    if submission is None or submission.state in _QUEUE_TERMINAL:
        return False
    board_cfg = settings.board_for_channel(submission.channel_id)
    if not _reaction_authorized(member, user_id, submission, board_cfg):
        return False

    req.confirmed_at = _now()
    req.confirmed_by = user_id
    submission.state = SubmissionState.QUEUED.value
    log.info("submission %s queued by %s via ✅ confirmation", submission.id, user_id)

    _snap, _atts, links = await _snapshot(session, submission)
    videos_added = 0
    if not submission.playlist_skipped:
        videos_added = await _auto_add_to_playlist(
            session, submission, links, board_cfg, yt_client
        )

    queue_url = (
        f"https://dashboard.exegesis.space/boards/{board_cfg.name}" if board_cfg else None
    )
    await channel.send(replies.queued_notice(
        bluesky_handle=board_cfg.bluesky_handle if board_cfg else None,
        dashboard_url=queue_url,
        youtube_playlist_id=board_cfg.youtube_playlist_id if board_cfg else None,
        videos_added=videos_added,
    ))
    if isinstance(channel, discord.Thread):
        if await _playlist_close_ready(
            session, submission.board_id,
            submission.source_discord_message_id, board_cfg,
            playlist_skipped=submission.playlist_skipped,
        ):
            _archive_thread_after_delay(channel, notice=replies.closing_notice("queued"))
    return True


def _is_curator(
    member: discord.Member | None,
    user_id: int,
    board_cfg: BoardConfig | None,
) -> bool:
    """True if user_id is an explicit curator user or holds a curator role."""
    if board_cfg is None:
        return False
    if user_id in board_cfg.curator_user_ids:
        return True
    if member is None:
        return False
    role_ids = {r.id for r in member.roles}
    return any(rid in role_ids for rid in board_cfg.curator_role_ids)


def _reaction_authorized(
    member: discord.Member | None,
    user_id: int,
    submission: Submission,
    board_cfg: BoardConfig | None,
) -> bool:
    if user_id == submission.author_id:
        return True
    return _is_curator(member, user_id, board_cfg)


async def _curator_authorized(
    channel: discord.abc.Messageable,
    user_id: int,
    board_cfg: BoardConfig | None,
) -> bool:
    """Check curator status for contexts where member object is unavailable (reaction remove)."""
    if board_cfg is None:
        return False
    if user_id in board_cfg.curator_user_ids:
        return True
    guild = getattr(channel, "guild", None)
    if guild is None:
        return False
    try:
        member = guild.get_member(user_id) or await guild.fetch_member(user_id)
    except (discord.NotFound, discord.HTTPException):
        return False
    role_ids = {r.id for r in member.roles}
    return any(rid in role_ids for rid in board_cfg.curator_role_ids)


async def handle_cancel_reaction(
    session: AsyncSession,
    *,
    settings: Settings,
    channel: discord.abc.Messageable,
    message_id: int,
    member: discord.Member | None,
    user_id: int,
) -> None:
    """❌ was reacted on a cancel-request message: delete the submission if authorized."""
    req = await session.scalar(
        select(CancellationRequest).where(CancellationRequest.bot_message_id == message_id)
    )
    if req is None:
        return

    submission = await session.get(Submission, req.submission_id)
    if submission is None:
        return

    board_cfg = settings.board_for_channel(submission.channel_id)
    if not _reaction_authorized(member, user_id, submission, board_cfg):
        return

    if submission.state == SubmissionState.PUBLISHED.value:
        thread = await _resolve_thread_by_id(channel, submission.thread_id) if submission.thread_id else None
        if thread is not None:
            attempt = await session.scalar(
                select(PublishAttempt)
                .where(PublishAttempt.submission_id == submission.id, PublishAttempt.success.is_(True))
                .order_by(PublishAttempt.attempted_at.desc())
            )
            bsky_url = attempt.bsky_url if attempt and attempt.bsky_url else "Bluesky"
            await thread.send(replies.cannot_remove_published(bsky_url))
        return

    sub_id = submission.id
    thread_id = submission.thread_id
    board = await session.get(Board, submission.board_id)
    remove_submission_dir(settings.attachments_dir, board.id if board else 0, sub_id)
    for model in (
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
    ):
        await session.execute(delete(model).where(model.submission_id == sub_id))
    await session.execute(delete(Submission).where(Submission.id == sub_id))
    log.info("deleted submission %s after ❌ cancel by user %s", sub_id, user_id)

    thread = await _resolve_thread_by_id(channel, thread_id) if thread_id else None
    if thread is not None:
        await thread.send(replies.reaction_removed())
        await _archive_thread(thread, notice=replies.closing_notice("submission cancelled"))


async def handle_source_cancel_reaction(
    session: AsyncSession,
    *,
    settings: Settings,
    channel: discord.abc.Messageable,
    message_id: int,
    member: discord.Member | None,
    user_id: int,
    yt_client=None,
) -> tuple[int | None, bool, list[str]]:
    """❌ reacted on the original source post: cancel the submission and/or playlist add if OP or curator.

    Returns (thread_id, cancelled_submission, removed_video_ids).
    thread_id is None if there's no thread to notify.
    """
    board = await _board_for_channel(session, channel.id)
    if board is None:
        return None
    board_cfg = settings.board_for_channel(channel.id)

    is_explicit_curator = board_cfg is not None and user_id in board_cfg.curator_user_ids
    is_role_curator = (
        member is not None
        and board_cfg is not None
        and any(r.id in board_cfg.curator_role_ids for r in member.roles)
    )

    thread_id: int | None = None
    cancelled_submission = False
    removed_video_ids: list[str] = []

    # Cancel any pending submission.
    submission = await session.scalar(
        select(Submission).where(
            Submission.board_id == board.id,
            Submission.source_discord_message_id == message_id,
        )
    )
    if submission is not None and submission.state != SubmissionState.PUBLISHED.value:
        is_op = user_id == submission.author_id
        if is_op or is_explicit_curator or is_role_curator:
            thread_id = thread_id or submission.thread_id
            sub_id = submission.id
            remove_submission_dir(settings.attachments_dir, board.id, sub_id)
            for model in (
                SourceRequest,
                AttachmentAltTextRequest,
                ContentLabelRequest,
                ImageRequest,
                MetadataRequest,
                SupplementalImageRequest,
                CancellationRequest,
                ConfirmationRequest,
                PublishAttempt,
                SubmissionLink,
                Attachment,
            ):
                await session.execute(delete(model).where(model.submission_id == sub_id))
            await session.execute(delete(Submission).where(Submission.id == sub_id))
            log.info("deleted submission %s after source-post ❌ by user %s", sub_id, user_id)
            cancelled_submission = True

    # Cancel any playlist addition(s) for this source message.
    playlist_rows = list(await session.scalars(
        select(YoutubePlaylistAdd).where(
            YoutubePlaylistAdd.board_id == board.id,
            YoutubePlaylistAdd.source_discord_message_id == message_id,
            YoutubePlaylistAdd.success.is_(True),
        )
    ))
    for row in playlist_rows:
        is_requester = user_id == row.discord_requester_id
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
        return None, False, []

    # Find thread_id from SubmissionThread if not already known.
    if thread_id is None:
        mapping = await session.scalar(
            select(SubmissionThread).where(
                SubmissionThread.board_id == board.id,
                SubmissionThread.source_discord_message_id == message_id,
            )
        )
        thread_id = mapping.thread_id if mapping else None

    return thread_id, cancelled_submission, removed_video_ids


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
    destination: discord.abc.Messageable,
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
    *,
    message_id: int,
    user_id: int,
    member: discord.Member | None,
    channel: discord.abc.Messageable,
    settings: Settings,
    yt_client,
) -> None:
    """⏹️ reacted on the playlist opt-out prompt: mark skipped and remove if already added."""
    submission = await session.scalar(
        select(Submission).where(Submission.playlist_opt_out_message_id == message_id)
    )
    if submission is None:
        return

    board = await session.get(Board, submission.board_id)
    board_cfg = settings.board_for_channel(board.discord_channel_id) if board else None

    is_op = user_id == submission.author_id
    if not (is_op or _is_curator(member, user_id, board_cfg)):
        return

    submission.playlist_skipped = True
    log.info("submission %s playlist opted out by user %s", submission.id, user_id)

    # Remove from playlist if auto-add already ran.
    playlist_rows = list(await session.scalars(
        select(YoutubePlaylistAdd).where(
            YoutubePlaylistAdd.board_id == submission.board_id,
            YoutubePlaylistAdd.source_discord_message_id == submission.source_discord_message_id,
            YoutubePlaylistAdd.success.is_(True),
        )
    ))
    for row in playlist_rows:
        await _do_playlist_remove(row, channel, session, yt_client)

    # If submission is QUEUED and thread is still open, it's now safe to archive.
    if submission.state == SubmissionState.QUEUED.value and submission.thread_id:
        resolved_thread = await _resolve_thread_by_id(channel, submission.thread_id)
        if resolved_thread is not None and not resolved_thread.archived:
            queued_at = submission.updated_at
            if queued_at is not None and queued_at.tzinfo is None:
                queued_at = queued_at.replace(tzinfo=timezone.utc)
            elapsed = (
                (datetime.now(timezone.utc) - queued_at).total_seconds()
                if queued_at else _THREAD_CLOSE_DELAY
            )
            remaining = max(0.0, _THREAD_CLOSE_DELAY - elapsed)
            _fire_and_forget(_archive_thread_after_delay_seconds(resolved_thread, remaining))


async def _resolve_thread_by_id(
    channel: discord.abc.Messageable, thread_id: int
) -> discord.Thread | None:
    guild = getattr(channel, "guild", None)
    if guild is None:
        return None
    cached = guild.get_thread(thread_id)
    if cached is not None:
        return cached
    try:
        resolved = await guild.fetch_channel(thread_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None
    return resolved if isinstance(resolved, discord.Thread) else None


async def _ingest_content(
    session: AsyncSession,
    submission: Submission,
    message: discord.Message,
    settings: Settings,
    http_client: httpx.AsyncClient,
) -> None:
    """One-time parse of links + embed capture + attachment download."""
    raw_urls = extract_urls(message.content)
    # Mobile share sheets send an embed URL without URL text in message.content.
    if not raw_urls:
        seen: set[str] = set()
        for embed in message.embeds:
            if embed.url and embed.url not in seen:
                seen.add(embed.url)
                raw_urls.append(embed.url)
    # Forwarded messages (HAS_SNAPSHOT flag) store content/embeds in message_snapshots.
    if not raw_urls:
        for snap in getattr(message, 'message_snapshots', []):
            snap_urls = extract_urls(getattr(snap, 'content', '') or '')
            if snap_urls:
                raw_urls.extend(snap_urls)
                break
            for embed in getattr(snap, 'embeds', []):
                if embed.url:
                    raw_urls.append(embed.url)
                    break
            if raw_urls:
                break
    for i, raw in enumerate(raw_urls):
        res = canonicalize(raw)
        session.add(
            SubmissionLink(
                submission_id=submission.id,
                order_index=i,
                raw_url=raw,
                canonical_url=res.canonical_url,
                domain_family=res.domain_family,
            )
        )

    _capture_embed(submission, message)

    all_attachments = list(message.attachments)
    for snap in getattr(message, 'message_snapshots', []):
        all_attachments.extend(getattr(snap, 'attachments', []))
    for att in all_attachments:
        await _ingest_attachment(session, submission, att, settings, http_client)


def _capture_embed(submission: Submission, message: discord.Message) -> None:
    """Store the Discord-generated link embed's title/description/thumb.

    Drives the external-embed preview and the at-least-one-image check. Embeds
    populate a beat after posting, so this may be empty if 🦋 was very fast.
    Also checks message_snapshots for forwarded messages.
    """
    all_embeds = list(message.embeds)
    for snap in getattr(message, 'message_snapshots', []):
        all_embeds.extend(getattr(snap, 'embeds', []))
    for embed in all_embeds:
        thumb = (embed.thumbnail.url if embed.thumbnail else None) or (
            embed.image.url if embed.image else None
        )
        if embed.title or embed.description or thumb:
            submission.embed_title = embed.title
            submission.embed_description = embed.description
            submission.embed_thumb_url = thumb
            return


async def _resolve_links(
    session: AsyncSession,
    submission: Submission,
    settings: Settings,
    http_client: httpx.AsyncClient,
) -> None:
    """Resolve per-link metadata and download each thumbnail to the volume.

    The primary (first) link falls back to the Discord-captured embed when our
    own fetch comes up empty.
    """
    links = list(
        (
            await session.scalars(
                select(SubmissionLink)
                .where(SubmissionLink.submission_id == submission.id)
                .order_by(SubmissionLink.order_index)
            )
        ).all()
    )
    for idx, link in enumerate(links):
        is_primary = idx == 0
        meta = await resolve(
            link.canonical_url,
            link.domain_family,
            client=http_client,
            fallback_title=submission.embed_title if is_primary else None,
            fallback_description=submission.embed_description if is_primary else None,
            fallback_image_url=submission.embed_thumb_url if is_primary else None,
            youtube_api_key=settings.youtube_api_key,
        )
        link.resolved_title = meta.title
        link.resolved_description = meta.description
        link.resolved_image_url = meta.image_url
        link.resolved_via = meta.via
        if meta.image_url:
            dest = submission_dir(settings.attachments_dir, submission.board_id, submission.id)
            try:
                link.resolved_image_path = await download_attachment(
                    url=meta.image_url,
                    dest_dir=dest,
                    filename=f"thumb_{link.id}",
                    data_dir=settings.data_dir,
                    min_free_mb=settings.storage_min_free_mb,
                    client=http_client,
                )
            except (StorageFullError, httpx.HTTPError, OSError) as exc:
                log.info("thumbnail download failed for link %s: %s", link.id, exc)


async def _ingest_attachment(
    session: AsyncSession,
    submission: Submission,
    att: discord.Attachment,
    settings: Settings,
    http_client: httpx.AsyncClient,
) -> Attachment:
    """Persist one Discord attachment row and download its bytes to the volume."""
    is_img = is_image_attachment(att.content_type, att.filename)
    is_vid = is_video_attachment(att.content_type, att.filename)
    status, body = initial_alt_text(is_image=is_img, is_video=is_vid, discord_description=att.description)
    row = Attachment(
        submission_id=submission.id,
        discord_attachment_id=att.id,
        filename=att.filename,
        discord_url=att.url,
        mime=att.content_type,
        width=att.width,
        height=att.height,
        spoiler=att.is_spoiler(),
        is_image=is_img,
        is_video=is_vid,
        alt_text_status=status.value,
        alt_text_body=body,
    )
    session.add(row)
    await session.flush()  # assign row.id
    dest = submission_dir(settings.attachments_dir, submission.board_id, submission.id)
    try:
        path = await download_attachment(
            url=att.url,
            dest_dir=dest,
            filename=f"{row.id}_{att.filename}",
            data_dir=settings.data_dir,
            min_free_mb=settings.storage_min_free_mb,
            client=http_client,
        )
        row.local_path = path
        row.downloaded_at = _now()
        if is_vid and row.local_path:
            row.local_path = await _transcode_video(row.local_path)
    except StorageFullError:
        log.warning(
            "storage full: attachment %s for submission %s not downloaded",
            att.id, submission.id,
        )
    except (httpx.HTTPError, OSError) as exc:
        log.warning("failed to download attachment %s: %s", att.id, exc)
    return row


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


# --- readiness evaluation + procedural requests -----------------------------


def _determine_kind(links: list[SubmissionLink], has_uploaded_image: bool, has_uploaded_video: bool = False) -> str:
    """Choose the Bluesky embed mode this submission would use."""
    first_family = links[0].domain_family if links else None
    if first_family == "bluesky":
        return "record"  # native repost/quote
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
    )
    return snap, atts, links


async def _has_open_request(session: AsyncSession, model, submission_id: int, **extra) -> bool:
    stmt = select(model).where(
        model.submission_id == submission_id, model.answered_at.is_(None)
    )
    for k, v in extra.items():
        stmt = stmt.where(getattr(model, k) == v)
    return (await session.scalar(stmt)) is not None


_DISCORD_MAX_BYTES = 8 * 1024 * 1024  # 8 MB free-tier upload limit
_ALT_PREVIEW_MAX_PX = 1920


def _discord_file_for_attachment(local_path: str, filename: str) -> discord.File:
    """Return a discord.File for the image, resizing in-memory if it exceeds 8 MB."""
    from PIL import Image

    with Image.open(local_path) as img:
        w, h = img.size
        if max(w, h) > _ALT_PREVIEW_MAX_PX:
            scale = _ALT_PREVIEW_MAX_PX / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        fmt = img.format or "JPEG"
        if fmt not in ("JPEG", "PNG", "WEBP", "GIF"):
            fmt = "JPEG"
        img.save(buf, format=fmt)
        buf.seek(0)
        if buf.getbuffer().nbytes > _DISCORD_MAX_BYTES:
            # Still too large after resize - re-encode as JPEG at reduced quality
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=70)
            buf.seek(0)
        return discord.File(buf, filename=filename)


_QUEUE_TERMINAL = frozenset({
    SubmissionState.QUEUED.value,
    SubmissionState.PUBLISHED.value,
    SubmissionState.PUBLISH_FAILED.value,
})

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
    _fire_and_forget(_archive_thread_after_delay_seconds(thread, _THREAD_CLOSE_DELAY, notice=notice))


async def _archive_thread(thread: discord.Thread, *, notice: str | None = None) -> None:
    """Immediately archive (close) a thread."""
    if thread.archived:
        return
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
    """Reopen an archived thread so the bot can post into it."""
    if not thread.archived:
        return
    try:
        await thread.edit(archived=False)
        log.debug("unarchived thread %s for reuse", thread.id)
    except Exception:
        log.warning("failed to unarchive thread %s", thread.id, exc_info=True)


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
    session: AsyncSession,
    submission: Submission,
    *,
    settings: Settings,
    destination: discord.abc.Messageable,
    yt_client=None,
    bot_id: int | None = None,
) -> SubmissionState:
    """Re-evaluate state and post any still-missing requests (idempotently).

    All procedural messages go into ``destination`` (the submission's thread).
    """
    old_state = submission.state
    snap, atts, links = await _snapshot(session, submission)
    new_state = evaluate_state(snap)
    gaps = set(missing_gaps(snap))
    # Don't overwrite state for submissions already past READY_TO_QUEUE - evaluate_state
    # is content-based and would otherwise downgrade QUEUED/PUBLISHED back to ready_to_queue.
    if old_state not in _QUEUE_TERMINAL:
        submission.state = new_state.value

    # Cancel button: posted once, before any other requests. OP and curators can react ❌.
    has_cancel = await session.scalar(
        select(CancellationRequest.id).where(CancellationRequest.submission_id == submission.id)
    ) is not None
    if not has_cancel:
        try:
            msg = await destination.send(replies.cancel_request())
            await msg.add_reaction(replies.CANCEL_EMOJI)
            session.add(CancellationRequest(
                submission_id=submission.id,
                bot_message_id=msg.id,
                prompted_at=_now(),
            ))
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("could not post cancel request for submission %s: %s", submission.id, exc)

    # Supplemental image offer: re-posted each time it's answered so OP/curator can
    # keep adding images in batches. Suppressed once the submission is terminal.
    if old_state not in _QUEUE_TERMINAL and not await _has_open_request(
        session, SupplementalImageRequest, submission.id
    ):
        try:
            msg = await destination.send(replies.supplemental_image_request())
            session.add(SupplementalImageRequest(
                submission_id=submission.id,
                bot_message_id=msg.id,
                prompted_at=_now(),
            ))
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("could not post supplemental image request for submission %s: %s", submission.id, exc)

    # Supplemental link offer: re-posted each time it's answered. Only shown once
    # a source link exists (SOURCE gap closed), so it doesn't compete with SourceRequest.
    if old_state not in _QUEUE_TERMINAL and snap.has_canonical_link and not await _has_open_request(
        session, SupplementalLinkRequest, submission.id
    ):
        try:
            msg = await destination.send(replies.supplemental_link_request())
            session.add(SupplementalLinkRequest(
                submission_id=submission.id,
                bot_message_id=msg.id,
                prompted_at=_now(),
            ))
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("could not post supplemental link request for submission %s: %s", submission.id, exc)

    if Gap.SOURCE in gaps and not await _has_open_request(
        session, SourceRequest, submission.id
    ):
        msg = await destination.send(replies.source_request())
        session.add(SourceRequest(submission_id=submission.id, bot_message_id=msg.id))

    if Gap.METADATA in gaps and not await _has_open_request(
        session, MetadataRequest, submission.id
    ):
        primary = _primary_link(links)
        url = primary.canonical_url if primary else "?"
        msg = await destination.send(replies.metadata_request(url))
        try:
            await msg.add_reaction(replies.METADATA_CONFIRM_EMOJI)
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("could not add metadata confirm reaction: %s", exc)
        session.add(MetadataRequest(submission_id=submission.id, bot_message_id=msg.id))

    # IMAGE gap is suppressed while METADATA is open - a better link may provide an image.
    if Gap.IMAGE in gaps and Gap.METADATA not in gaps and not await _has_open_request(
        session, ImageRequest, submission.id
    ):
        msg = await destination.send(replies.image_request())
        session.add(ImageRequest(submission_id=submission.id, bot_message_id=msg.id))

    if Gap.ALT_TEXT in gaps:
        for att in atts:
            if not (att.is_image or att.is_video) or att.alt_text_status != AltTextStatus.NEEDED.value:
                continue
            if await _has_open_request(
                session, AttachmentAltTextRequest, submission.id, attachment_id=att.id
            ):
                continue
            if att.local_path and att.is_image:
                file = _discord_file_for_attachment(att.local_path, att.filename)
                msg = await destination.send(replies.alt_text_request(att.filename), file=file)
            else:
                msg = await destination.send(
                    replies.alt_text_request(att.filename) + f"\n{att.discord_url}"
                )
            session.add(
                AttachmentAltTextRequest(
                    submission_id=submission.id,
                    attachment_id=att.id,
                    bot_message_id=msg.id,
                )
            )

    has_graphic_notice = await session.scalar(
        select(ContentLabelRequest.id).where(ContentLabelRequest.submission_id == submission.id)
    ) is not None
    if snap.graphic_classification_required and not has_graphic_notice:
        msg = await destination.send(replies.graphic_request())
        try:
            await msg.add_reaction(GRAPHIC_YES_EMOJI)
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("could not seed 🩸 reaction: %s", exc)
        session.add(
            ContentLabelRequest(submission_id=submission.id, bot_message_id=msg.id)
        )

    action = _queue_action(old_state, new_state)
    if action in ("fresh", "silent"):
        has_conf = await session.scalar(
            select(ConfirmationRequest.id).where(
                ConfirmationRequest.submission_id == submission.id
            )
        ) is not None
        if not has_conf:
            preview = await _build_post_preview(session, submission, atts, links)
            for page in replies.format_post_preview(preview):
                await destination.send(page)
            board_cfg_conf = settings.board_for_channel(submission.channel_id)
            msg = await destination.send(replies.confirmation_request(
                bluesky_handle=board_cfg_conf.bluesky_handle if board_cfg_conf else None,
                youtube_playlist_id=board_cfg_conf.youtube_playlist_id if board_cfg_conf else None,
            ))
            try:
                await msg.add_reaction(replies.CONFIRMATION_EMOJI)
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning("could not seed ✅ on confirmation: %s", exc)
            session.add(ConfirmationRequest(
                submission_id=submission.id,
                bot_message_id=msg.id,
            ))

    return new_state


async def publish_queued_submission(
    session: AsyncSession,
    settings: Settings,
    submission: Submission,
    destination: discord.abc.Messageable | None,
) -> None:
    """Called by the scheduler to publish a QUEUED or PUBLISH_FAILED submission.

    Loads current attachments and links from DB, then delegates to _attempt_publish.
    ``destination`` is the submission thread (or None if the thread can't be resolved -
    publish still proceeds, just without a Discord status notice).
    """
    _snap, atts, links = await _snapshot(session, submission)
    if destination is None:
        class _DevNull:
            async def send(self, *a, **kw):
                pass
        destination = _DevNull()  # type: ignore[assignment]
    await _attempt_publish(session, settings, submission, atts, links, destination)


async def _attempt_publish(
    session: AsyncSession,
    settings: Settings,
    submission: Submission,
    atts: list[Attachment],
    links: list[SubmissionLink],
    destination: discord.abc.Messageable,
) -> None:
    """Publish to Bluesky and record the result. Fires once per ready transition."""
    board_cfg = settings.board_for_channel(submission.channel_id)
    if not board_cfg or not board_cfg.bluesky_handle:
        log.info(
            "submission %s ready but board has no bluesky_handle - skipping publish",
            submission.id,
        )
        return

    password = settings.bsky_password_for(board_cfg.name)
    if not password:
        log.warning(
            "submission %s ready but no app password for board %s - skipping publish",
            submission.id, board_cfg.name,
        )
        return

    result = await publisher.publish_submission(
        submission=submission,
        links=links,
        attachments=atts,
        board_cfg=board_cfg,
        password=password,
    )
    session.add(
        PublishAttempt(
            submission_id=submission.id,
            success=result.success,
            at_uri=result.at_uri,
            at_cid=result.at_cid,
            bsky_url=result.bsky_url,
            error=result.error,
        )
    )
    if result.success and result.at_uri:
        submission.state = SubmissionState.PUBLISHED.value
        bsky_url = result.bsky_url or publisher.at_uri_to_url(result.at_uri)
        if result.is_repost:
            await destination.send(replies.reposted_notice(bsky_url))
        else:
            await destination.send(replies.published_notice(bsky_url))
        log.info("submission %s published: %s", submission.id, result.at_uri)
        if isinstance(destination, discord.Thread):
            _archive_thread_after_delay(destination, notice=replies.closing_notice("published to Bluesky"))
    else:
        submission.state = SubmissionState.PUBLISH_FAILED.value
        await destination.send(replies.publish_failed_notice(result.error))
        log.error("submission %s publish failed: %s", submission.id, result.error)


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

    return replies.PostPreview(
        kind=kind,
        title=primary.resolved_title if primary else None,
        links=[(link.canonical_url, link.domain_family) for link in links],
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
    )


# --- human reply handling ---------------------------------------------------


def _is_authorized(
    author: discord.Member | discord.User,
    submission: Submission,
    board_cfg: BoardConfig | None,
) -> bool:
    if author.id == submission.author_id:
        return True
    if board_cfg is None:
        return False
    role_ids = {r.id for r in getattr(author, "roles", [])}
    return any(rid in role_ids for rid in board_cfg.curator_role_ids)


async def handle_reply(
    session: AsyncSession,
    *,
    settings: Settings,
    message: discord.Message,
    http_client: httpx.AsyncClient,
    yt_client=None,
) -> bool:
    """If ``message`` answers one of our open requests, apply it. Returns handled?"""
    ref = message.reference
    if ref is None or ref.message_id is None:
        return False
    bot_msg_id = ref.message_id

    req = None
    for model in (SourceRequest, AttachmentAltTextRequest, ImageRequest, MetadataRequest, SupplementalImageRequest, SupplementalLinkRequest):
        req = await session.scalar(select(model).where(model.bot_message_id == bot_msg_id))
        if req is not None:
            break
    if req is None:
        return False  # reply to something that isn't one of our prompts

    submission = await session.get(Submission, req.submission_id)
    if submission is None:
        return False
    board_cfg = settings.board_for_channel(submission.channel_id)
    if not _is_authorized(message.author, submission, board_cfg):
        return False  # silently ignore non-curators

    if req.answered_at is not None:
        return True  # already satisfied; ignore duplicate

    handled = await _apply_answer(session, req, submission, message, settings, http_client)
    if not handled:
        return True  # we replied with a nudge; leave request open

    # Replies arrive in the submission's thread, so post follow-ups right there.
    await recompute_and_request(session, submission, settings=settings, destination=message.channel, yt_client=yt_client)
    return True


async def _apply_answer(
    session: AsyncSession,
    req,
    submission: Submission,
    message: discord.Message,
    settings: Settings,
    http_client: httpx.AsyncClient,
) -> bool:
    """Apply a single reply. Returns False if the answer was unusable (nudged)."""
    if isinstance(req, (ImageRequest, SupplementalImageRequest)):
        image_atts = [
            a for a in message.attachments
            if is_image_attachment(a.content_type, a.filename)
            or is_video_attachment(a.content_type, a.filename)
        ]
        if not image_atts:
            await message.reply(replies.media_not_found(), mention_author=False)
            return False
        for att in image_atts:
            await _ingest_attachment(session, submission, att, settings, http_client)

    elif isinstance(req, SourceRequest):
        urls = extract_urls(message.content)
        if not urls:
            await message.reply(replies.source_not_found(), mention_author=False)
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
        await _resolve_links(session, submission, settings, http_client)

    elif isinstance(req, SupplementalLinkRequest):
        urls = extract_urls(message.content)
        if not urls:
            await message.reply(replies.supplemental_link_not_found(), mention_author=False)
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
        await _resolve_links(session, submission, settings, http_client)

    elif isinstance(req, AttachmentAltTextRequest):
        body = (message.content or "").strip()
        if not body:
            return False
        att = await session.get(Attachment, req.attachment_id)
        if att is not None:
            att.alt_text_body = body
            att.alt_text_status = AltTextStatus.PROVIDED.value
            att.alt_text_author = message.author.id

    elif isinstance(req, MetadataRequest):
        urls = extract_urls(message.content)
        if not urls:
            await message.reply(replies.metadata_url_not_found(), mention_author=False)
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
        await message.reply(replies.metadata_link_updated(canon.canonical_url), mention_author=False)
        await _resolve_links(session, submission, settings, http_client)

    req.answer = message.content
    req.answered_by = message.author.id
    req.answered_at = _now()
    return True
