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
    handle_source_note_confirm,
    handle_source_note_reject,
    recompute_and_request,
    skip_all_alt_text,
    waive_source,
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


# --- skip all alt text (/skip_alt service fn) -------------------------------


async def test_skip_all_alt_marks_all_needed(session, board):
    sub = make_submission(board, state=SubmissionState.AWAITING_ALT_TEXT.value, author_id=999)
    session.add(sub)
    await session.flush()
    a1 = await _img(session, sub)
    a2 = await _img(session, sub)
    session.add(AttachmentAltTextRequest(submission_id=sub.id, attachment_id=a1.id, bot_message_id=next(_ids)))
    session.add(AttachmentAltTextRequest(submission_id=sub.id, attachment_id=a2.id, bot_message_id=next(_ids)))
    await session.flush()

    dest = MockDest()
    count = await skip_all_alt_text(session, sub, settings=_settings(), user_id=999, destination=dest)

    assert count == 2  # noqa: PLR2004
    for a in (a1, a2):
        await session.refresh(a)
        assert a.alt_text_status == AltTextStatus.SKIPPED.value
        assert a.alt_text_author == 999
    reqs = list(await session.scalars(
        select(AttachmentAltTextRequest).where(AttachmentAltTextRequest.submission_id == sub.id)
    ))
    assert all(r.answered_at is not None and r.answer == "skipped" for r in reqs)
    assert any("skipped" in m.lower() for m in dest.sent)


async def test_skip_all_alt_leaves_resolved_alone(session, board):
    sub = make_submission(board, state=SubmissionState.AWAITING_ALT_TEXT.value, author_id=999)
    session.add(sub)
    await session.flush()
    needed = await _img(session, sub)
    provided = await _img(session, sub, status=AltTextStatus.PROVIDED.value)

    dest = MockDest()
    count = await skip_all_alt_text(session, sub, settings=_settings(), user_id=999, destination=dest)

    assert count == 1
    await session.refresh(needed)
    await session.refresh(provided)
    assert needed.alt_text_status == AltTextStatus.SKIPPED.value
    assert provided.alt_text_status == AltTextStatus.PROVIDED.value  # untouched


async def test_skip_all_alt_nothing_pending_returns_zero(session, board):
    sub = make_submission(board, state=SubmissionState.READY_TO_QUEUE.value, author_id=999)
    session.add(sub)
    await session.flush()
    await _img(session, sub, status=AltTextStatus.PROVIDED.value)

    dest = MockDest()
    count = await skip_all_alt_text(session, sub, settings=_settings(), user_id=999, destination=dest)

    assert count == 0
    assert dest.sent == []  # nothing pending -> no notice, no recompute posts


# --- no known source (/no_source service fn) --------------------------------


async def test_waive_source_waives(session, board):
    sub = make_submission(board, state=SubmissionState.AWAITING_SOURCE.value, author_id=999)
    session.add(sub)
    await session.flush()
    await _img(session, sub, status=AltTextStatus.PROVIDED.value)
    session.add(SourceRequest(submission_id=sub.id, bot_message_id=next(_ids)))
    await session.flush()

    dest = MockDest()
    waived = await waive_source(session, sub, settings=_settings(), user_id=999, destination=dest)

    assert waived is True
    await session.refresh(sub)
    assert sub.source_waived is True
    req = await session.scalar(select(SourceRequest).where(SourceRequest.submission_id == sub.id))
    assert req.answered_at is not None and req.answer == "no_source"
    assert any("source unknown" in m for m in dest.sent)


async def test_waive_source_already_waived_returns_false(session, board):
    sub = make_submission(board, state=SubmissionState.READY_TO_QUEUE.value,
                          author_id=999, source_waived=True)
    session.add(sub)
    await session.flush()

    dest = MockDest()
    waived = await waive_source(session, sub, settings=_settings(), user_id=999, destination=dest)

    assert waived is False
    assert dest.sent == []  # no-op, no notice


# --- non-URL source note confirmation --------------------------------------


async def test_source_note_confirm_commits_note(session, board):
    sub = make_submission(board, state=SubmissionState.AWAITING_SOURCE.value, author_id=999)
    sub.source_note = "Popular Mechanics, March 1965"
    sub.source_note_confirmed = False
    session.add(sub)
    await session.flush()
    await _img(session, sub, status=AltTextStatus.PROVIDED.value)
    session.add(SourceRequest(submission_id=sub.id, bot_message_id=next(_ids)))
    await session.flush()

    inter = _interaction(999)
    await handle_source_note_confirm(session, inter, sub.id, _settings())

    await session.refresh(sub)
    assert sub.source_note_confirmed is True
    req = await session.scalar(select(SourceRequest).where(SourceRequest.submission_id == sub.id))
    assert req.answered_at is not None and req.answer == "source_note"
    assert any("Popular Mechanics" in m for m in inter.channel.sent)


async def test_source_note_confirm_unauthorized(session, board):
    sub = make_submission(board, state=SubmissionState.AWAITING_SOURCE.value, author_id=1)
    sub.source_note = "an old catalog"
    session.add(sub)
    await session.flush()

    inter = _interaction(555)
    await handle_source_note_confirm(session, inter, sub.id, _settings())

    await session.refresh(sub)
    assert sub.source_note_confirmed is False
    inter.followup.send.assert_awaited_once()


async def test_source_note_confirm_without_candidate(session, board):
    sub = make_submission(board, state=SubmissionState.AWAITING_SOURCE.value, author_id=999)
    session.add(sub)
    await session.flush()
    inter = _interaction(999)
    await handle_source_note_confirm(session, inter, sub.id, _settings())
    inter.followup.send.assert_awaited_once()  # nothing to confirm


async def test_source_note_confirm_missing_submission(session, board):
    inter = _interaction(999)
    await handle_source_note_confirm(session, inter, 999999, _settings())
    inter.followup.send.assert_not_awaited()


async def test_source_note_reject_discards(session, board):
    sub = make_submission(board, state=SubmissionState.AWAITING_SOURCE.value, author_id=999)
    sub.source_note = "misfired chatter"
    sub.source_note_confirmed = False
    session.add(sub)
    await session.flush()

    inter = _interaction(999)
    await handle_source_note_reject(session, inter, sub.id, _settings())

    await session.refresh(sub)
    assert sub.source_note is None
    assert sub.source_note_confirmed is False
    assert any("discarded" in m.lower() for m in inter.channel.sent)


async def test_source_note_reject_unauthorized(session, board):
    sub = make_submission(board, state=SubmissionState.AWAITING_SOURCE.value, author_id=1)
    sub.source_note = "keep me"
    session.add(sub)
    await session.flush()
    inter = _interaction(555)
    await handle_source_note_reject(session, inter, sub.id, _settings())
    await session.refresh(sub)
    assert sub.source_note == "keep me"  # unauthorized: unchanged
    inter.followup.send.assert_awaited_once()


async def test_source_note_reject_missing_submission(session, board):
    inter = _interaction(999)
    await handle_source_note_reject(session, inter, 999999, _settings())
    inter.followup.send.assert_not_awaited()


async def test_source_note_confirm_without_open_request(session, board):
    # No open SourceRequest row (e.g. offered via checklist only); confirmation still applies.
    sub = make_submission(board, state=SubmissionState.AWAITING_SOURCE.value, author_id=999)
    sub.source_note = "an old catalog"
    session.add(sub)
    await session.flush()
    await handle_source_note_confirm(session, _interaction(999), sub.id, _settings())
    await session.refresh(sub)
    assert sub.source_note_confirmed is True


async def test_source_note_confirm_channel_none_returns_after_commit(session, board):
    sub = make_submission(board, state=SubmissionState.AWAITING_SOURCE.value, author_id=999)
    sub.source_note = "an old catalog"
    session.add(sub)
    await session.flush()
    inter = _interaction(999)
    inter.channel = None
    await handle_source_note_confirm(session, inter, sub.id, _settings())
    # committed even with nowhere to post the notice (in-memory: no recompute/flush on this path)
    assert sub.source_note_confirmed is True


async def test_source_note_confirm_tombstone_edit_error_swallowed(session, board):
    sub = make_submission(board, state=SubmissionState.AWAITING_SOURCE.value, author_id=999)
    sub.source_note = "an old catalog"
    session.add(sub)
    await session.flush()
    inter = _interaction(999)
    inter.message.edit = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "boom"))
    await handle_source_note_confirm(session, inter, sub.id, _settings())  # must not raise
    await session.refresh(sub)
    assert sub.source_note_confirmed is True


async def test_source_note_reject_channel_none_returns_after_discard(session, board):
    sub = make_submission(board, state=SubmissionState.AWAITING_SOURCE.value, author_id=999)
    sub.source_note = "misfire"
    session.add(sub)
    await session.flush()
    inter = _interaction(999)
    inter.channel = None
    await handle_source_note_reject(session, inter, sub.id, _settings())
    assert sub.source_note is None  # in-memory: no recompute/flush on the channel-None path


async def test_source_note_reject_tombstone_edit_error_swallowed(session, board):
    sub = make_submission(board, state=SubmissionState.AWAITING_SOURCE.value, author_id=999)
    sub.source_note = "misfire"
    session.add(sub)
    await session.flush()
    inter = _interaction(999)
    inter.message.edit = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "boom"))
    await handle_source_note_reject(session, inter, sub.id, _settings())  # must not raise
    await session.refresh(sub)
    assert sub.source_note is None


async def test_waive_source_without_open_request(session, board):
    # No open SourceRequest row (e.g. offered via checklist only); waiver still applies.
    sub = make_submission(board, state=SubmissionState.AWAITING_SOURCE.value, author_id=999)
    session.add(sub)
    await session.flush()
    await _img(session, sub, status=AltTextStatus.PROVIDED.value)

    waived = await waive_source(session, sub, settings=_settings(), user_id=999, destination=MockDest())
    assert waived is True
    await session.refresh(sub)
    assert sub.source_waived is True


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


# --- recompute mentions the /no_source waiver only with media ---------------


async def test_recompute_source_prompt_mentions_waiver_with_media(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value)
    session.add(sub)
    await session.flush()
    await _img(session, sub, status=AltTextStatus.PROVIDED.value)  # media, but no link -> SOURCE gap

    dest = MockDest()
    await recompute_and_request(session, sub, settings=_settings(), destination=dest)
    assert any("/no_source" in m for m in dest.sent)  # waiver hinted only when there's media


async def test_recompute_plain_source_prompt_without_media(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value)
    session.add(sub)
    await session.flush()  # no media, no link -> SOURCE gap, no waiver hinted

    dest = MockDest()
    await recompute_and_request(session, sub, settings=_settings(), destination=dest)
    assert any("source URL" in m for m in dest.sent)
    assert not any("/no_source" in m for m in dest.sent)


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
