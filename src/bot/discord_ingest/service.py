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
from datetime import datetime, timezone

import discord
import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..accessibility import initial_alt_text, is_image_attachment
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
from .. import publish as publisher
from ..moderation import (
    GRAPHIC_NO_EMOJI,
    GRAPHIC_YES_EMOJI,
    graphic_from_emoji,
    parse_graphic_answer,
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
) -> None:
    """Entry point for a 🦋 reaction on a watched channel message."""
    board = await _board_for_channel(session, message.channel.id)
    if board is None:
        return  # not a watched channel

    if not skip_auth:
        board_cfg = settings.board_for_channel(message.channel.id)
        if not _is_curator(member, user_id, board_cfg):
            return

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

    thread = await _ensure_thread(session, settings, message, submission)
    if thread is None:
        log.warning("could not create/resolve thread for submission %s", submission.id)
        return

    if created:
        links = list(await session.scalars(
            select(SubmissionLink).where(SubmissionLink.submission_id == submission.id)
        ))
        for link in links:
            prior = await _find_prior_post(session, link.canonical_url, submission.id)
            if prior:
                await thread.send(replies.duplicate_warning(prior))
                break

    await recompute_and_request(session, submission, settings=settings, destination=thread)


async def _ensure_thread(
    session: AsyncSession,
    settings: Settings,
    message: discord.Message,
    submission: Submission,
) -> discord.Thread | None:
    """Get (or create) the per-submission *private* thread.

    Reuse is keyed by a durable SubmissionThread mapping (survives 🦋 removal), so
    re-reacting reuses the same thread without re-pinging curators.
    """
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
            return existing

    # Create a new private thread (no channel-visible "started a thread" system message).
    try:
        name = await _derive_thread_title(session, message, submission)
        thread = await message.channel.create_thread(  # type: ignore[union-attr]
            name=name,
            type=discord.ChannelType.private_thread,
            invitable=False,
        )
    except (discord.Forbidden, discord.HTTPException) as exc:
        log.warning("private thread creation failed for message %s: %s", message.id, exc)
        return None

    await _post_thread_anchor(settings, message, submission, thread)

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
    return thread


async def _post_thread_anchor(
    settings: Settings,
    message: discord.Message,
    submission: Submission,
    thread: discord.Thread,
) -> None:
    """Anchor the private thread: add curator users, notify curator roles, forward the source message."""
    cfg = settings.board_for_channel(submission.channel_id)
    curator_user_ids = cfg.curator_user_ids if cfg else []
    text = replies.thread_anchor(
        author_mention=f"<@{submission.author_id}>",
        curator_user_mentions=[f"<@{uid}>" for uid in curator_user_ids],
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
        CancellationRequest,
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
    await recompute_and_request(session, submission, settings=settings, destination=channel)


async def handle_metadata_reaction(
    session: AsyncSession,
    *,
    settings: Settings,
    channel: discord.abc.Messageable,
    message_id: int,
    member: discord.Member | None,
    user_id: int,
) -> None:
    """A curator reacted 🔗 on a metadata-request message — confirm this is the best link."""
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
    await recompute_and_request(session, submission, settings=settings, destination=channel)


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
        CancellationRequest,
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


async def handle_source_cancel_reaction(
    session: AsyncSession,
    *,
    settings: Settings,
    channel: discord.abc.Messageable,
    message_id: int,
    member: discord.Member | None,
    user_id: int,
) -> int | None:
    """❌ reacted on the original source post: cancel the submission if OP or curator.

    Returns the thread_id to send a confirmation message to, or None if the
    reaction should be ignored (not a submission, not authorized, already published).
    """
    board = await _board_for_channel(session, channel.id)
    if board is None:
        return None
    board_cfg = settings.board_for_channel(channel.id)

    submission = await session.scalar(
        select(Submission).where(
            Submission.board_id == board.id,
            Submission.source_discord_message_id == message_id,
        )
    )
    if submission is None:
        return None

    if submission.state == SubmissionState.PUBLISHED.value:
        return None

    is_op = user_id == submission.author_id
    is_explicit_curator = board_cfg is not None and user_id in board_cfg.curator_user_ids
    is_role_curator = (
        member is not None
        and board_cfg is not None
        and any(r.id in board_cfg.curator_role_ids for r in member.roles)
    )
    if not (is_op or is_explicit_curator or is_role_curator):
        return None

    sub_id = submission.id
    thread_id = submission.thread_id
    remove_submission_dir(settings.attachments_dir, board.id, sub_id)
    for model in (
        SourceRequest,
        AttachmentAltTextRequest,
        ContentLabelRequest,
        ImageRequest,
        MetadataRequest,
        CancellationRequest,
        PublishAttempt,
        SubmissionLink,
        Attachment,
    ):
        await session.execute(delete(model).where(model.submission_id == sub_id))
    await session.execute(delete(Submission).where(Submission.id == sub_id))
    log.info("deleted submission %s after source-post ❌ by user %s", sub_id, user_id)
    return thread_id


async def handle_playlist_reaction(
    session: AsyncSession,
    *,
    settings: Settings,
    message: discord.Message,
    board_cfg: BoardConfig,
    yt_client,
    reactor_id: int,
) -> None:
    """▶️ reacted by a curator: add the YouTube video to the board's playlist."""
    from ..resolve.fetch import _youtube_video_id

    if yt_client is None:
        await message.channel.send("YouTube playlist not configured")
        return

    urls = extract_urls(message.content)
    video_ids: list[str] = []
    seen: set[str] = set()
    for raw in urls:
        try:
            result = canonicalize(raw)
        except Exception:
            continue
        if result.domain_family != "youtube":
            continue
        vid = _youtube_video_id(result.canonical_url)
        if vid and vid not in seen:
            video_ids.append(vid)
            seen.add(vid)

    if not video_ids:
        await message.channel.send("no YouTube video link found")
        return

    board = await _board_for_channel(session, message.channel.id)
    if board is None:
        return

    for video_id in video_ids:
        existing = await session.scalar(
            select(YoutubePlaylistAdd).where(
                YoutubePlaylistAdd.board_id == board.id,
                YoutubePlaylistAdd.video_id == video_id,
                YoutubePlaylistAdd.success.is_(True),
            )
        )
        if existing is not None:
            await message.channel.send(f"▶️ already in playlist: https://youtu.be/{video_id}")
            continue

        playlist_id = board_cfg.youtube_playlist_id
        item_id: str | None = None
        error_msg: str | None = None
        success = False
        try:
            loop = asyncio.get_running_loop()
            item_id = await loop.run_in_executor(
                None, yt_client.add_to_playlist, playlist_id, video_id
            )
            success = True
        except Exception as exc:
            error_msg = str(exc)
            log.warning("playlist insert failed for video %s: %s", video_id, exc)

        session.add(YoutubePlaylistAdd(
            board_id=board.id,
            source_discord_message_id=message.id,
            video_id=video_id,
            playlist_id=playlist_id,
            discord_requester_id=reactor_id,
            success=success,
            error_message=error_msg,
        ))

        if success:
            await message.channel.send(f"▶️ added to playlist: https://youtu.be/{video_id}")
        else:
            await message.channel.send(f"failed to add https://youtu.be/{video_id} to playlist: {error_msg}")


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
    for i, raw in enumerate(extract_urls(message.content)):
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

    for att in message.attachments:
        await _ingest_attachment(session, submission, att, settings, http_client)


def _capture_embed(submission: Submission, message: discord.Message) -> None:
    """Store the Discord-generated link embed's title/description/thumb.

    Drives the external-embed preview and the at-least-one-image check. Embeds
    populate a beat after posting, so this may be empty if 🦋 was very fast.
    """
    for embed in message.embeds:
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
    status, body = initial_alt_text(is_image=is_img, discord_description=att.description)
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
    except StorageFullError:
        log.warning(
            "storage full: attachment %s for submission %s not downloaded",
            att.id, submission.id,
        )
    except (httpx.HTTPError, OSError) as exc:
        log.warning("failed to download attachment %s: %s", att.id, exc)
    return row


# --- readiness evaluation + procedural requests -----------------------------


def _determine_kind(links: list[SubmissionLink], has_uploaded_image: bool) -> str:
    """Choose the Bluesky embed mode this submission would use."""
    first_family = links[0].domain_family if links else None
    if first_family == "bluesky":
        return "record"  # native repost/quote
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
    kind = _determine_kind(links, has_uploaded_image)
    primary = _primary_link(links)
    has_embed_image = bool(primary.resolved_image_path) if primary else False
    image_statuses = [AltTextStatus(a.alt_text_status) for a in atts if a.is_image]
    resolved_via = primary.resolved_via if primary else None
    confirmed_meta = await session.scalar(
        select(MetadataRequest).where(
            MetadataRequest.submission_id == submission.id,
            MetadataRequest.answer == "confirmed",
        )
    )
    snap = SubmissionSnapshot(
        has_canonical_link=len(links) > 0,
        image_alt_statuses=image_statuses,
        graphic_status=GraphicStatus(submission.graphic_status),
        graphic_classification_required=submission.graphic_classification_required,
        needs_image=kind in ("images", "external"),
        has_image=has_uploaded_image or has_embed_image,
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

_MEMBER_REMOVAL_DELAY = 15 * 60  # seconds after queuing before removing humans from thread


async def _remove_thread_members_after_delay(thread: discord.Thread) -> None:
    """Remove all human members from a private thread after a short grace period.

    Leaves the bot in the thread so it can post the publish confirmation later.
    Runs as a fire-and-forget background task.
    """
    await asyncio.sleep(_MEMBER_REMOVAL_DELAY)
    try:
        bot_id = thread.guild.me.id
        members = await thread.fetch_members()
        for member in members:
            if member.id != bot_id:
                await thread.remove_user(discord.Object(id=member.id))
        log.debug("removed %d member(s) from queued thread %s", len(members) - 1, thread.id)
    except Exception:
        log.warning("failed to remove members from thread %s", thread.id, exc_info=True)


def _queue_action(old_state: str, evaluated: SubmissionState) -> str:
    """Decide what to do when evaluate_state returns READY_TO_QUEUE.

    Returns one of:
      "fresh"  — first time reaching READY_TO_QUEUE; post confirmation + queue
      "silent" — was stuck at READY_TO_QUEUE; transition to QUEUED without reposting
      "none"   — already queued/published/failed; no state change
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
) -> SubmissionState:
    """Re-evaluate state and post any still-missing requests (idempotently).

    All procedural messages go into ``destination`` (the submission's thread).
    """
    old_state = submission.state
    snap, atts, links = await _snapshot(session, submission)
    new_state = evaluate_state(snap)
    gaps = set(missing_gaps(snap))
    # Don't overwrite state for submissions already past READY_TO_QUEUE — evaluate_state
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

    # IMAGE gap is suppressed while METADATA is open — a better link may provide an image.
    if Gap.IMAGE in gaps and Gap.METADATA not in gaps and not await _has_open_request(
        session, ImageRequest, submission.id
    ):
        msg = await destination.send(replies.image_request())
        session.add(ImageRequest(submission_id=submission.id, bot_message_id=msg.id))

    if Gap.ALT_TEXT in gaps:
        for att in atts:
            if not att.is_image or att.alt_text_status != AltTextStatus.NEEDED.value:
                continue
            if await _has_open_request(
                session, AttachmentAltTextRequest, submission.id, attachment_id=att.id
            ):
                continue
            if att.local_path:
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

    if Gap.GRAPHIC in gaps and not await _has_open_request(
        session, ContentLabelRequest, submission.id
    ):
        msg = await destination.send(replies.graphic_request())
        # Pre-seed the yes/no reactions so a curator just clicks one.
        try:
            await msg.add_reaction(GRAPHIC_YES_EMOJI)
            await msg.add_reaction(GRAPHIC_NO_EMOJI)
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("could not add graphic-vote reactions: %s", exc)
        session.add(
            ContentLabelRequest(submission_id=submission.id, bot_message_id=msg.id)
        )

    action = _queue_action(old_state, new_state)
    if action != "none":
        if action == "fresh":
            await destination.send(replies.ready_confirmation())
            preview = await _build_post_preview(session, submission, atts, links)
            await destination.send(replies.format_post_preview(preview))
            _board_cfg = settings.board_for_channel(submission.channel_id)
            queue_url = (
                f"https://dashboard.exegesis.space/boards/{_board_cfg.name}"
                if _board_cfg else None
            )
            await destination.send(replies.queued_notice(queue_url))
        submission.state = SubmissionState.QUEUED.value
        log.info("submission %s queued", submission.id)
        if isinstance(destination, discord.Thread):
            asyncio.create_task(_remove_thread_members_after_delay(destination))

    return new_state


async def publish_queued_submission(
    session: AsyncSession,
    settings: Settings,
    submission: Submission,
    destination: discord.abc.Messageable | None,
) -> None:
    """Called by the scheduler to publish a QUEUED or PUBLISH_FAILED submission.

    Loads current attachments and links from DB, then delegates to _attempt_publish.
    ``destination`` is the submission thread (or None if the thread can't be resolved —
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
    kind = _determine_kind(links, has_uploaded_image)
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
) -> bool:
    """If ``message`` answers one of our open requests, apply it. Returns handled?"""
    ref = message.reference
    if ref is None or ref.message_id is None:
        return False
    bot_msg_id = ref.message_id

    req = None
    for model in (SourceRequest, AttachmentAltTextRequest, ContentLabelRequest, ImageRequest, MetadataRequest):
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
    await recompute_and_request(session, submission, settings=settings, destination=message.channel)
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
    if isinstance(req, ImageRequest):
        image_atts = [
            a for a in message.attachments
            if is_image_attachment(a.content_type, a.filename)
        ]
        if not image_atts:
            await message.reply(replies.image_not_found(), mention_author=False)
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

    elif isinstance(req, AttachmentAltTextRequest):
        body = (message.content or "").strip()
        if not body:
            return False
        att = await session.get(Attachment, req.attachment_id)
        if att is not None:
            att.alt_text_body = body
            att.alt_text_status = AltTextStatus.PROVIDED.value
            att.alt_text_author = message.author.id

    elif isinstance(req, ContentLabelRequest):
        status = parse_graphic_answer(message.content)
        if status is None:
            await message.reply(replies.graphic_not_understood(), mention_author=False)
            return False
        submission.graphic_status = status.value

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
