"""Tests for the skip-alt-text and no-known-source escape hatches, plus the
status-checklist edit-in-place plumbing they run through.
"""

from __future__ import annotations

import itertools
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from sqlalchemy import select

from bot.config import BoardConfig
from bot.discord_ingest import service
from bot.discord_ingest.service import (
    handle_alt_skip_button,
    handle_no_source_button,
    recompute_and_request,
    _edit_status_message,
    render_submission_status,
)
from bot.models import Attachment, AttachmentAltTextRequest, SourceRequest, SubmissionLink
from bot.state import AltTextStatus, SubmissionState

from conftest import MockDest, make_submission

_ids = itertools.count(70_000)


def _settings(curator_ids=None):
    cfg = BoardConfig(
        name="robots", discord_guild_id=1, discord_channel_id=100,
        bluesky_handle="robots.exegesis.space", tags=[],
        curator_user_ids=curator_ids or [],
    )
    s = MagicMock()
    s.board_for_channel.return_value = cfg
    s.bsky_password_for.return_value = "pw"
    s.queue_target_days = 90
    s.queue_min_daily = 1
    s.queue_max_daily = 6
    return s


def _interaction(user_id: int):
    inter = MagicMock(spec=discord.Interaction)
    inter.user = MagicMock()
    inter.user.id = user_id
    inter.user.roles = []
    inter.channel = MockDest()  # destination for recompute: send + get_partial_message + archive
    inter.message = MagicMock()
    inter.message.edit = AsyncMock()
    inter.followup = MagicMock()
    inter.followup.send = AsyncMock()
    return inter


async def _img(session, sub, *, status=AltTextStatus.NEEDED.value):
    att = Attachment(
        submission_id=sub.id, discord_attachment_id=next(_ids), filename="robot.jpg",
        discord_url="https://cdn.discord.com/robot.jpg", is_image=True, is_video=False,
        alt_text_status=status,
    )
    session.add(att)
    await session.flush()
    return att


# --- skip alt text ----------------------------------------------------------


async def test_alt_skip_op_marks_skipped(session, board):
    sub = make_submission(board, state=SubmissionState.AWAITING_ALT_TEXT.value, author_id=999)
    session.add(sub)
    await session.flush()
    att = await _img(session, sub)
    session.add(AttachmentAltTextRequest(
        submission_id=sub.id, attachment_id=att.id, bot_message_id=next(_ids),
    ))
    await session.flush()

    await handle_alt_skip_button(session, _interaction(999), att.id, _settings())

    await session.refresh(att)
    assert att.alt_text_status == AltTextStatus.SKIPPED.value
    assert att.alt_text_author == 999
    req = await session.scalar(
        select(AttachmentAltTextRequest).where(AttachmentAltTextRequest.attachment_id == att.id)
    )
    assert req.answered_at is not None and req.answer == "skipped"


async def test_alt_skip_curator_allowed(session, board):
    sub = make_submission(board, state=SubmissionState.AWAITING_ALT_TEXT.value, author_id=1)
    session.add(sub)
    await session.flush()
    att = await _img(session, sub)

    await handle_alt_skip_button(session, _interaction(555), att.id, _settings(curator_ids=[555]))

    await session.refresh(att)
    assert att.alt_text_status == AltTextStatus.SKIPPED.value


async def test_alt_skip_unauthorized_rejected(session, board):
    sub = make_submission(board, state=SubmissionState.AWAITING_ALT_TEXT.value, author_id=1)
    session.add(sub)
    await session.flush()
    att = await _img(session, sub)

    inter = _interaction(555)  # not OP, not curator
    await handle_alt_skip_button(session, inter, att.id, _settings())

    await session.refresh(att)
    assert att.alt_text_status == AltTextStatus.NEEDED.value
    inter.followup.send.assert_awaited_once()


async def test_alt_skip_attachment_missing(session, board):
    inter = _interaction(999)
    await handle_alt_skip_button(session, inter, 999999, _settings())
    inter.followup.send.assert_awaited_once()


async def test_alt_skip_already_handled(session, board):
    sub = make_submission(board, state=SubmissionState.READY_TO_QUEUE.value, author_id=999)
    session.add(sub)
    await session.flush()
    att = await _img(session, sub, status=AltTextStatus.PROVIDED.value)

    inter = _interaction(999)
    await handle_alt_skip_button(session, inter, att.id, _settings())

    await session.refresh(att)
    assert att.alt_text_status == AltTextStatus.PROVIDED.value
    inter.followup.send.assert_awaited_once()


# --- no known source --------------------------------------------------------


async def test_no_source_op_waives(session, board):
    sub = make_submission(board, state=SubmissionState.AWAITING_SOURCE.value, author_id=999)
    session.add(sub)
    await session.flush()
    await _img(session, sub, status=AltTextStatus.PROVIDED.value)
    session.add(SourceRequest(submission_id=sub.id, bot_message_id=next(_ids)))
    await session.flush()

    inter = _interaction(999)
    await handle_no_source_button(session, inter, sub.id, _settings())

    await session.refresh(sub)
    assert sub.source_waived is True
    req = await session.scalar(select(SourceRequest).where(SourceRequest.submission_id == sub.id))
    assert req.answered_at is not None and req.answer == "no_source"
    # A "source unknown" notice was posted to the thread.
    assert any("source unknown" in m for m in inter.channel.sent)


async def test_no_source_unauthorized(session, board):
    sub = make_submission(board, state=SubmissionState.AWAITING_SOURCE.value, author_id=1)
    session.add(sub)
    await session.flush()

    inter = _interaction(555)
    await handle_no_source_button(session, inter, sub.id, _settings())

    await session.refresh(sub)
    assert sub.source_waived is False
    inter.followup.send.assert_awaited_once()


async def test_no_source_already_waived(session, board):
    sub = make_submission(board, state=SubmissionState.READY_TO_QUEUE.value,
                          author_id=999, source_waived=True)
    session.add(sub)
    await session.flush()

    inter = _interaction(999)
    await handle_no_source_button(session, inter, sub.id, _settings())
    inter.followup.send.assert_awaited_once()


async def test_no_source_missing_submission(session, board):
    inter = _interaction(999)
    await handle_no_source_button(session, inter, 999999, _settings())
    # No crash; nothing to do.
    inter.followup.send.assert_not_awaited()


async def test_no_source_without_open_request_still_waives(session, board):
    # No open SourceRequest row (e.g. offered via checklist only); waiver still applies.
    sub = make_submission(board, state=SubmissionState.AWAITING_SOURCE.value, author_id=999)
    session.add(sub)
    await session.flush()
    await _img(session, sub, status=AltTextStatus.PROVIDED.value)

    await handle_no_source_button(session, _interaction(999), sub.id, _settings())
    await session.refresh(sub)
    assert sub.source_waived is True


async def test_no_source_channel_none_returns_after_waiving(session, board):
    sub = make_submission(board, state=SubmissionState.AWAITING_SOURCE.value, author_id=999)
    session.add(sub)
    await session.flush()
    inter = _interaction(999)
    inter.channel = None
    await handle_no_source_button(session, inter, sub.id, _settings())
    await session.refresh(sub)
    assert sub.source_waived is True  # waiver persisted even with nowhere to post the notice


async def test_no_source_tombstone_edit_error_swallowed(session, board):
    sub = make_submission(board, state=SubmissionState.AWAITING_SOURCE.value, author_id=999)
    session.add(sub)
    await session.flush()
    await _img(session, sub, status=AltTextStatus.PROVIDED.value)
    inter = _interaction(999)
    inter.message.edit = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "boom"))
    await handle_no_source_button(session, inter, sub.id, _settings())  # must not raise
    await session.refresh(sub)
    assert sub.source_waived is True


async def test_alt_skip_submission_gone(session, board):
    # Attachment whose submission row is absent (FK not enforced in the test DB).
    att = Attachment(
        submission_id=999999, discord_attachment_id=next(_ids), filename="x.jpg",
        discord_url="https://x", is_image=True, alt_text_status=AltTextStatus.NEEDED.value,
    )
    session.add(att)
    await session.flush()
    inter = _interaction(999)
    await handle_alt_skip_button(session, inter, att.id, _settings())  # returns, no crash


async def test_alt_skip_channel_none_returns_after_marking(session, board):
    sub = make_submission(board, state=SubmissionState.AWAITING_ALT_TEXT.value, author_id=999)
    session.add(sub)
    await session.flush()
    att = await _img(session, sub)
    inter = _interaction(999)
    inter.channel = None
    await handle_alt_skip_button(session, inter, att.id, _settings())
    await session.refresh(att)
    assert att.alt_text_status == AltTextStatus.SKIPPED.value


async def test_alt_skip_tombstone_edit_error_swallowed(session, board):
    sub = make_submission(board, state=SubmissionState.AWAITING_ALT_TEXT.value, author_id=999)
    session.add(sub)
    await session.flush()
    att = await _img(session, sub)
    inter = _interaction(999)
    inter.message.edit = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "boom"))
    await handle_alt_skip_button(session, inter, att.id, _settings())  # must not raise
    await session.refresh(att)
    assert att.alt_text_status == AltTextStatus.SKIPPED.value


async def test_ready_keeps_checklist_and_puts_confirmation_last(session, board):
    from bot.models import ConfirmationRequest
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value)
    session.add(sub)
    await session.flush()
    session.add(SubmissionLink(
        submission_id=sub.id, order_index=0, raw_url="https://x/y",
        canonical_url="https://x/y", domain_family="other", resolved_image_path="/t.jpg",
        resolved_via="opengraph",  # no metadata gap -> reaches READY_TO_QUEUE
    ))
    await session.flush()

    dest = MockDest()
    await recompute_and_request(session, sub, settings=_settings(), destination=dest)

    # Ready: the checklist is kept (all checked, "Ready to queue"), and the queue
    # confirmation is the LAST message so the buttons sit at the bottom, after the preview.
    assert any("post status" in m and "Ready to queue" in m for m in dest.sent)
    assert "queue this for posting" in dest.sent[-1]
    preview_idx = next(i for i, m in enumerate(dest.sent) if "prospective Bluesky post" in m)
    conf_idx = next(i for i, m in enumerate(dest.sent) if "queue this for posting" in m)
    assert preview_idx < conf_idx  # preview before the queue confirmation
    # The confirmation is a standalone message; the checklist persists for posterity.
    conf = await session.scalar(
        select(ConfirmationRequest).where(ConfirmationRequest.submission_id == sub.id)
    )
    assert conf is not None
    assert sub.status_message_id is not None


# --- recompute offers the waiver button only with media ---------------------


async def test_recompute_offers_no_source_button_with_media(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value)
    session.add(sub)
    await session.flush()
    await _img(session, sub, status=AltTextStatus.PROVIDED.value)  # media, but no link -> SOURCE gap

    dest = MockDest()
    with patch("bot.discord_ingest.service.views.make_no_source_view") as mk:
        await recompute_and_request(session, sub, settings=_settings(), destination=dest)
    mk.assert_called_once_with(sub.id)
    assert any("No known source" in m for m in dest.sent)


async def test_recompute_plain_source_prompt_without_media(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value)
    session.add(sub)
    await session.flush()  # no media, no link -> SOURCE gap, no waiver offered

    dest = MockDest()
    with patch("bot.discord_ingest.service.views.make_no_source_view") as mk:
        await recompute_and_request(session, sub, settings=_settings(), destination=dest)
    mk.assert_not_called()
    assert any("with the source URL" in m for m in dest.sent)


# --- render_submission_status (used by /status) -----------------------------


async def test_render_submission_status_blocked(session, board):
    sub = make_submission(board, state=SubmissionState.AWAITING_SOURCE.value)
    session.add(sub)
    await session.flush()
    out = await render_submission_status(session, sub)
    assert "Not queued yet" in out


async def test_render_submission_status_queued_terminal(session, board):
    sub = make_submission(board, state=SubmissionState.QUEUED.value)
    session.add(sub)
    await session.flush()
    session.add(SubmissionLink(
        submission_id=sub.id, order_index=0, raw_url="https://x/y",
        canonical_url="https://x/y", domain_family="other",
    ))
    await session.flush()
    out = await render_submission_status(session, sub)
    assert "Queued" in out


# --- _edit_status_message edge cases ----------------------------------------


async def test_edit_status_message_no_getter_returns_false():
    dest = MagicMock(spec=[])  # no get_partial_message
    assert await _edit_status_message(dest, 1, "content", None) is False


async def test_edit_status_message_not_found_returns_false():
    dest = MagicMock()
    partial = MagicMock()
    partial.edit = AsyncMock(side_effect=discord.NotFound(MagicMock(), "gone"))
    dest.get_partial_message = MagicMock(return_value=partial)
    assert await _edit_status_message(dest, 1, "content", None) is False


async def test_edit_status_message_http_error_returns_true():
    dest = MagicMock()
    partial = MagicMock()
    partial.edit = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "boom"))
    dest.get_partial_message = MagicMock(return_value=partial)
    # A transient edit error is swallowed (True = handled, don't respawn).
    assert await _edit_status_message(dest, 1, "content", None) is True


class _SendOnlyDest:
    """A destination that can only send (no get_partial_message) - forces the
    upsert to fall back to sending a fresh checklist instead of editing."""

    def __init__(self):
        self.sent: list[str] = []

    async def send(self, content=None, **kw):
        self.sent.append(content or "")
        m = MagicMock()
        m.id = next(_ids)
        return m

    async def archive(self, notice):
        pass


async def test_upsert_falls_back_to_send_when_edit_unsupported(session, board):
    # status_message_id is set but the destination can't edit -> a fresh checklist is sent.
    # A blocked (no-source) submission renders the checklist, not the confirmation prompt.
    sub = make_submission(board, state=SubmissionState.AWAITING_SOURCE.value, status_message_id=42)
    session.add(sub)
    await session.flush()

    dest = _SendOnlyDest()
    await recompute_and_request(session, sub, settings=_settings(), destination=dest)
    assert any("post status" in m for m in dest.sent)
