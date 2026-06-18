"""DB orchestration for the ingestion loop.

Pure-ish service layer: it owns the submission lifecycle (create, ingest content,
recompute readiness, post/answer procedural requests) and talks to Discord only
to post replies and read attachments. Kept separate from the event wiring in
client.py so the logic is easy to follow and extend toward Matrix later.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import discord
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..accessibility import initial_alt_text, is_image_attachment
from ..asset_store import StorageFullError, download_attachment, submission_dir
from ..canonicalize import canonicalize
from ..config import BoardConfig, Settings
from ..models import (
    AttachmentAltTextRequest,
    Attachment,
    Board,
    ContentLabelRequest,
    SourceRequest,
    Submission,
    SubmissionLink,
)
from ..moderation import parse_graphic_answer
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


# --- submission creation + content ingest -----------------------------------


async def handle_reaction(
    session: AsyncSession,
    *,
    settings: Settings,
    message: discord.Message,
    http_client: httpx.AsyncClient,
) -> None:
    """Entry point for a 🦋 reaction on a watched channel message."""
    board = await _board_for_channel(session, message.channel.id)
    if board is None:
        return  # not a watched channel

    submission = await session.scalar(
        select(Submission).where(
            Submission.board_id == board.id,
            Submission.source_discord_message_id == message.id,
        )
    )
    created = submission is None
    if created:
        submission = Submission(
            board_id=board.id,
            source_discord_message_id=message.id,
            channel_id=message.channel.id,
            author_id=message.author.id,
            author_display=getattr(message.author, "display_name", str(message.author)),
            state=SubmissionState.INTENT_SUBMITTED.value,
        )
        session.add(submission)
        await session.flush()  # assign submission.id
        await _ingest_content(session, submission, message, settings, http_client)
        log.info("created submission %s for message %s", submission.id, message.id)

    await recompute_and_request(session, submission, source_message=message)


async def _ingest_content(
    session: AsyncSession,
    submission: Submission,
    message: discord.Message,
    settings: Settings,
    http_client: httpx.AsyncClient,
) -> None:
    """One-time parse of links + download of attachments for a new submission."""
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

    dest = submission_dir(settings.attachments_dir, submission.board_id, submission.id)
    for att in message.attachments:
        is_img = is_image_attachment(att.content_type, att.filename)
        status, body = initial_alt_text(
            is_image=is_img, discord_description=att.description
        )
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
        await session.flush()  # assign row.id for logging / future linkage
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
                att.id,
                submission.id,
            )
        except (httpx.HTTPError, OSError) as exc:
            log.warning("failed to download attachment %s: %s", att.id, exc)


# --- readiness evaluation + procedural requests -----------------------------


async def _snapshot(
    session: AsyncSession, submission: Submission
) -> tuple[SubmissionSnapshot, list[Attachment]]:
    links = (
        await session.scalars(
            select(SubmissionLink).where(SubmissionLink.submission_id == submission.id)
        )
    ).all()
    atts = (
        await session.scalars(
            select(Attachment).where(Attachment.submission_id == submission.id)
        )
    ).all()
    image_statuses = [
        AltTextStatus(a.alt_text_status) for a in atts if a.is_image
    ]
    snap = SubmissionSnapshot(
        has_canonical_link=len(links) > 0,
        image_alt_statuses=image_statuses,
        graphic_status=GraphicStatus(submission.graphic_status),
        graphic_classification_required=submission.graphic_classification_required,
    )
    return snap, list(atts)


async def _has_open_request(session: AsyncSession, model, submission_id: int, **extra) -> bool:
    stmt = select(model).where(
        model.submission_id == submission_id, model.answered_at.is_(None)
    )
    for k, v in extra.items():
        stmt = stmt.where(getattr(model, k) == v)
    return (await session.scalar(stmt)) is not None


async def recompute_and_request(
    session: AsyncSession,
    submission: Submission,
    *,
    source_message: discord.Message,
) -> SubmissionState:
    """Re-evaluate state and post any still-missing requests (idempotently)."""
    old_state = submission.state
    snap, atts = await _snapshot(session, submission)
    new_state = evaluate_state(snap)
    submission.state = new_state.value
    gaps = set(missing_gaps(snap))
    mention = source_message.author.mention

    if Gap.SOURCE in gaps and not await _has_open_request(
        session, SourceRequest, submission.id
    ):
        msg = await source_message.reply(
            replies.source_request(mention), mention_author=True
        )
        session.add(SourceRequest(submission_id=submission.id, bot_message_id=msg.id))

    if Gap.ALT_TEXT in gaps:
        for att in atts:
            if not att.is_image or att.alt_text_status != AltTextStatus.NEEDED.value:
                continue
            if await _has_open_request(
                session, AttachmentAltTextRequest, submission.id, attachment_id=att.id
            ):
                continue
            msg = await source_message.reply(
                replies.alt_text_request(mention, att.filename), mention_author=True
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
        msg = await source_message.reply(
            replies.graphic_request(mention), mention_author=True
        )
        session.add(
            ContentLabelRequest(submission_id=submission.id, bot_message_id=msg.id)
        )

    if (
        new_state == SubmissionState.READY_TO_QUEUE
        and old_state != SubmissionState.READY_TO_QUEUE.value
    ):
        await source_message.reply(replies.ready_confirmation(), mention_author=False)
        log.info("submission %s is ready_to_queue", submission.id)

    return new_state


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
) -> bool:
    """If ``message`` answers one of our open requests, apply it. Returns handled?"""
    ref = message.reference
    if ref is None or ref.message_id is None:
        return False
    bot_msg_id = ref.message_id

    source_req = await session.scalar(
        select(SourceRequest).where(SourceRequest.bot_message_id == bot_msg_id)
    )
    alt_req = await session.scalar(
        select(AttachmentAltTextRequest).where(
            AttachmentAltTextRequest.bot_message_id == bot_msg_id
        )
    )
    graphic_req = await session.scalar(
        select(ContentLabelRequest).where(
            ContentLabelRequest.bot_message_id == bot_msg_id
        )
    )
    req = source_req or alt_req or graphic_req
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

    handled = await _apply_answer(session, req, submission, message)
    if not handled:
        return True  # we replied with a nudge; leave request open

    # Re-evaluate against the original source message so follow-ups thread correctly.
    source_message = await _fetch_source_message(message, submission)
    if source_message is not None:
        await recompute_and_request(session, submission, source_message=source_message)
    return True


async def _apply_answer(
    session: AsyncSession,
    req,
    submission: Submission,
    message: discord.Message,
) -> bool:
    """Apply a single reply. Returns False if the answer was unusable (nudged)."""
    if isinstance(req, SourceRequest):
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

    req.answer = message.content
    req.answered_by = message.author.id
    req.answered_at = _now()
    return True


async def _fetch_source_message(
    reply_message: discord.Message, submission: Submission
) -> discord.Message | None:
    try:
        return await reply_message.channel.fetch_message(
            submission.source_discord_message_id
        )
    except (discord.NotFound, discord.HTTPException) as exc:
        log.warning("could not fetch source message for submission %s: %s", submission.id, exc)
        return None
