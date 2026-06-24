"""Integration tests for handle_reply() and _apply_answer() routing.

These tests use a real in-memory SQLite database (via the shared session fixture)
and mock Discord messages. They cover:

  - Routing: each request type dispatches to the correct handler
  - Authorization: non-OP, non-curator replies are silently dropped
  - Alt text: empty replies nudge, non-empty replies are recorded
  - Source URL: URL extraction creates SubmissionLink rows
  - Supplemental image: image attachments are ingested, non-image attachments nudge
  - Answered requests: duplicate replies are ignored
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from bot.discord_ingest import replies
from bot.discord_ingest.service import handle_reply
from bot.models import (
    Attachment,
    AttachmentAltTextRequest,
    Board,
    SourceRequest,
    Submission,
    SubmissionLink,
    SupplementalImageRequest,
)
from bot.state import AltTextStatus, SubmissionState

from conftest import make_submission

_NEXT_ID = iter(range(90_000, 100_000))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_settings(board_cfg=None, curator_role_ids=None, curator_user_ids=None):
    s = MagicMock()
    if board_cfg is not None:
        s.board_for_channel.return_value = board_cfg
    else:
        cfg = MagicMock()
        cfg.curator_role_ids = curator_role_ids or []
        cfg.curator_user_ids = curator_user_ids or []
        s.board_for_channel.return_value = cfg
    s.dashboard_url = None
    s.attachments_dir = "/tmp/test-attachments"
    s.storage_min_free_mb = 0
    return s


def _make_message(
    *,
    reply_to_id: int,
    author_id: int = 999,
    content: str = "",
    attachments: list | None = None,
    roles: list | None = None,
) -> MagicMock:
    msg = MagicMock()
    msg.reference = MagicMock()
    msg.reference.message_id = reply_to_id
    msg.author = MagicMock()
    msg.author.id = author_id
    msg.author.roles = roles or []
    msg.content = content
    msg.attachments = attachments or []
    msg.reply = AsyncMock()
    msg.channel = MagicMock()
    msg.channel.send = AsyncMock(return_value=_make_discord_msg())
    return msg


def _make_discord_msg() -> MagicMock:
    m = MagicMock()
    m.id = next(_NEXT_ID)
    m.add_reaction = AsyncMock()
    return m


def _make_image_attachment(filename: str = "robot.jpg") -> MagicMock:
    att = MagicMock()
    att.id = next(_NEXT_ID)
    att.filename = filename
    att.content_type = "image/jpeg"
    att.url = f"https://cdn.discord.com/{filename}"
    att.description = None
    att.width = 800
    att.height = 600
    att.is_spoiler.return_value = False
    return att


async def _add_source_request(session, submission: Submission) -> SourceRequest:
    req = SourceRequest(
        submission_id=submission.id,
        bot_message_id=next(_NEXT_ID),
    )
    session.add(req)
    await session.flush()
    return req


async def _add_alt_text_request(session, submission: Submission, attachment_id: int) -> AttachmentAltTextRequest:
    req = AttachmentAltTextRequest(
        submission_id=submission.id,
        attachment_id=attachment_id,
        bot_message_id=next(_NEXT_ID),
    )
    session.add(req)
    await session.flush()
    return req


async def _add_supplemental_request(session, submission: Submission) -> SupplementalImageRequest:
    req = SupplementalImageRequest(
        submission_id=submission.id,
        bot_message_id=next(_NEXT_ID),
    )
    session.add(req)
    await session.flush()
    return req


# ---------------------------------------------------------------------------
# Non-reply messages are ignored
# ---------------------------------------------------------------------------

async def test_handle_reply_non_reply_returns_false(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()

    msg = MagicMock()
    msg.reference = None  # not a reply
    settings = _mock_settings()

    result = await handle_reply(session, settings=settings, message=msg, http_client=MagicMock())
    assert result is False


# ---------------------------------------------------------------------------
# Authorization: only OP or curators can answer
# ---------------------------------------------------------------------------

async def test_handle_reply_unauthorized_user_ignored(session, board):
    sub = make_submission(board, author_id=100)
    session.add(sub)
    await session.flush()
    req = await _add_source_request(session, sub)

    msg = _make_message(reply_to_id=req.bot_message_id, author_id=999)  # neither OP nor curator
    settings = _mock_settings(curator_role_ids=[], curator_user_ids=[])

    result = await handle_reply(session, settings=settings, message=msg, http_client=MagicMock())

    assert result is False
    # request stays open
    fresh = await session.get(SourceRequest, req.id)
    assert fresh.answered_at is None


async def test_handle_reply_op_is_always_authorized(session, board):
    sub = make_submission(board, author_id=100)
    session.add(sub)
    await session.flush()
    req = await _add_source_request(session, sub)

    msg = _make_message(reply_to_id=req.bot_message_id, author_id=100, content="https://example.com/post")
    settings = _mock_settings(curator_role_ids=[], curator_user_ids=[])

    with patch("bot.discord_ingest.service._resolve_links", new=AsyncMock()):
        result = await handle_reply(session, settings=settings, message=msg, http_client=MagicMock())

    assert result is True


async def test_handle_reply_curator_by_role_is_authorized(session, board):
    sub = make_submission(board, author_id=100)
    session.add(sub)
    await session.flush()
    req = await _add_source_request(session, sub)

    curator_role = MagicMock()
    curator_role.id = 555
    msg = _make_message(
        reply_to_id=req.bot_message_id,
        author_id=999,
        content="https://example.com/post",
        roles=[curator_role],
    )
    settings = _mock_settings(curator_role_ids=[555], curator_user_ids=[])

    with patch("bot.discord_ingest.service._resolve_links", new=AsyncMock()):
        result = await handle_reply(session, settings=settings, message=msg, http_client=MagicMock())

    assert result is True


# ---------------------------------------------------------------------------
# Alt text request: empty reply nudges, non-empty reply is stored
# ---------------------------------------------------------------------------

async def test_handle_reply_alt_text_empty_nudges(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    att = Attachment(
        submission_id=sub.id,
        discord_attachment_id=next(_NEXT_ID),
        filename="robot.jpg",
        discord_url="https://cdn.discord.com/robot.jpg",
        is_image=True,
        alt_text_status=AltTextStatus.NEEDED.value,
    )
    session.add(att)
    await session.flush()
    req = await _add_alt_text_request(session, sub, att.id)

    msg = _make_message(reply_to_id=req.bot_message_id, author_id=sub.author_id, content="   ")
    settings = _mock_settings()

    result = await handle_reply(session, settings=settings, message=msg, http_client=MagicMock())

    # Returns True (we handled it by nudging), request stays open
    assert result is True
    fresh_req = await session.get(AttachmentAltTextRequest, req.id)
    assert fresh_req.answered_at is None
    fresh_att = await session.get(Attachment, att.id)
    assert fresh_att.alt_text_status == AltTextStatus.NEEDED.value


async def test_handle_reply_alt_text_stored(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    att = Attachment(
        submission_id=sub.id,
        discord_attachment_id=next(_NEXT_ID),
        filename="robot.jpg",
        discord_url="https://cdn.discord.com/robot.jpg",
        is_image=True,
        alt_text_status=AltTextStatus.NEEDED.value,
    )
    session.add(att)
    await session.flush()
    req = await _add_alt_text_request(session, sub, att.id)

    msg = _make_message(
        reply_to_id=req.bot_message_id,
        author_id=sub.author_id,
        content="a chrome robot arm reaching toward the camera",
    )
    settings = _mock_settings()

    # Patch recompute to avoid Discord send calls
    with patch("bot.discord_ingest.service.recompute_and_request", new=AsyncMock()):
        result = await handle_reply(session, settings=settings, message=msg, http_client=MagicMock())

    assert result is True
    fresh_att = await session.get(Attachment, att.id)
    assert fresh_att.alt_text_status == AltTextStatus.PROVIDED.value
    assert fresh_att.alt_text_body == "a chrome robot arm reaching toward the camera"
    assert fresh_att.alt_text_author == sub.author_id


# ---------------------------------------------------------------------------
# Source request: URL creates SubmissionLink
# ---------------------------------------------------------------------------

async def test_handle_reply_source_url_creates_link(session, board):
    sub = make_submission(board, state=SubmissionState.AWAITING_SOURCE.value)
    session.add(sub)
    await session.flush()
    req = await _add_source_request(session, sub)

    msg = _make_message(
        reply_to_id=req.bot_message_id,
        author_id=sub.author_id,
        content="https://www.artstation.com/artwork/cool-robot",
    )
    settings = _mock_settings()

    with patch("bot.discord_ingest.service._resolve_links", new=AsyncMock()), \
         patch("bot.discord_ingest.service.recompute_and_request", new=AsyncMock()):
        result = await handle_reply(session, settings=settings, message=msg, http_client=MagicMock())

    assert result is True
    links = list(await session.scalars(
        select(SubmissionLink).where(SubmissionLink.submission_id == sub.id)
    ))
    assert len(links) == 1
    assert "artstation.com" in links[0].canonical_url


async def test_handle_reply_source_no_url_nudges(session, board):
    sub = make_submission(board, state=SubmissionState.AWAITING_SOURCE.value)
    session.add(sub)
    await session.flush()
    req = await _add_source_request(session, sub)

    msg = _make_message(
        reply_to_id=req.bot_message_id,
        author_id=sub.author_id,
        content="here is my art",  # no URL
    )
    settings = _mock_settings()

    result = await handle_reply(session, settings=settings, message=msg, http_client=MagicMock())

    assert result is True  # handled (nudge sent), request stays open
    msg.reply.assert_called_once()
    fresh_req = await session.get(SourceRequest, req.id)
    assert fresh_req.answered_at is None


# ---------------------------------------------------------------------------
# Supplemental image request
# ---------------------------------------------------------------------------

async def test_handle_reply_supplemental_image_ingests_images(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    req = await _add_supplemental_request(session, sub)

    discord_att = _make_image_attachment("extra_robot.jpg")
    msg = _make_message(
        reply_to_id=req.bot_message_id,
        author_id=sub.author_id,
        attachments=[discord_att],
    )
    settings = _mock_settings()

    with patch("bot.discord_ingest.service._ingest_attachment", new=AsyncMock()) as mock_ingest, \
         patch("bot.discord_ingest.service.recompute_and_request", new=AsyncMock()):
        result = await handle_reply(session, settings=settings, message=msg, http_client=MagicMock())

    assert result is True
    mock_ingest.assert_called_once()


async def test_handle_reply_supplemental_image_no_image_nudges(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    req = await _add_supplemental_request(session, sub)

    # Non-image attachment
    pdf_att = MagicMock()
    pdf_att.filename = "document.pdf"
    pdf_att.content_type = "application/pdf"

    msg = _make_message(
        reply_to_id=req.bot_message_id,
        author_id=sub.author_id,
        attachments=[pdf_att],
    )
    settings = _mock_settings()

    result = await handle_reply(session, settings=settings, message=msg, http_client=MagicMock())

    assert result is True  # handled (nudge sent)
    msg.reply.assert_called_once()
    fresh_req = await session.get(SupplementalImageRequest, req.id)
    assert fresh_req.answered_at is None  # stays open


# ---------------------------------------------------------------------------
# Already-answered requests are ignored
# ---------------------------------------------------------------------------

async def test_handle_reply_already_answered_ignored(session, board):
    from datetime import datetime, timezone
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    req = await _add_source_request(session, sub)

    # Mark it as answered
    req.answered_at = datetime.now(timezone.utc)
    req.answer = "https://old.example.com"
    await session.flush()

    msg = _make_message(
        reply_to_id=req.bot_message_id,
        author_id=sub.author_id,
        content="https://new.example.com",
    )
    settings = _mock_settings()

    result = await handle_reply(session, settings=settings, message=msg, http_client=MagicMock())

    # Returns True (recognized as ours) but does not process
    assert result is True
    links = list(await session.scalars(
        select(SubmissionLink).where(SubmissionLink.submission_id == sub.id)
    ))
    assert len(links) == 0  # no new link created


# ---------------------------------------------------------------------------
# Reply to an unknown bot message is ignored
# ---------------------------------------------------------------------------

async def test_handle_reply_unknown_bot_message_returns_false(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()

    msg = _make_message(reply_to_id=99999, author_id=sub.author_id)
    settings = _mock_settings()

    result = await handle_reply(session, settings=settings, message=msg, http_client=MagicMock())
    assert result is False
