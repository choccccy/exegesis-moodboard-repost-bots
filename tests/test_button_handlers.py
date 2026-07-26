"""Tests for button/reaction handlers in service.py."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from sqlalchemy import select

from bot.config import BoardConfig
from bot.discord_ingest.service import (
    apply_post_edits,
    handle_cancel_button,
    handle_confirm_button,
    handle_confirmation_reaction,
    handle_edit_button,
    handle_graphic_button,
    handle_label_reaction,
    handle_metadata_confirm_button,
    handle_metadata_reaction,
    handle_playlist_skip_button,
)
from bot.models import (
    Attachment,
    CancellationRequest,
    ConfirmationRequest,
    ContentLabelRequest,
    MetadataRequest,
    PublishAttempt,
    Submission,
    SubmissionLink,
    YoutubePlaylistAdd,
)
from bot.curation.outcomes import Ack, Noop, OpenModal, Tombstone
from bot.state import AltTextStatus, GraphicStatus, SubmissionState

from conftest import MockDest, make_submission


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------

CURATOR_ID = 42
AUTHOR_ID = 999


def _board_cfg(**kw) -> BoardConfig:
    defaults = dict(
        name="robots",
        discord_guild_id=1,
        discord_channel_id=100,
        require_graphic_classification=False,
        curator_user_ids=[CURATOR_ID],
    )
    defaults.update(kw)
    return BoardConfig(**defaults)


def _settings(**kw) -> MagicMock:
    s = MagicMock()
    s.board_for_channel.return_value = _board_cfg(**kw)
    s.trigger_emoji = "🦋"
    s.attachments_dir = "/tmp/test-attachments"
    return s


def _event(user_id: int = CURATOR_ID, submission_id: int = 0, *, values=()):
    """A normalized button/select event (member=None; curator authz here is by id)."""
    from bot.curation.events import InteractionEvent
    return InteractionEvent(
        user_id=user_id, submission_id=submission_id, member=None, values=tuple(values)
    )


def _interaction(user_id: int = CURATOR_ID, channel: object = None) -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    user = MagicMock()
    user.id = user_id
    interaction.user = user
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()
    interaction.message = MagicMock()
    interaction.message.edit = AsyncMock()
    interaction.channel = channel or _thread_mock()
    interaction.client = MagicMock()
    interaction.client.get_channel.return_value = None
    interaction.client.fetch_channel = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "no"))
    return interaction


def _thread_mock(thread_id: int = 500) -> MagicMock:
    t = MagicMock(spec=discord.Thread)
    t.id = thread_id
    t.archived = False
    t.send = AsyncMock()
    t.edit = AsyncMock()
    t.guild = MagicMock()
    return t


def _channel_mock() -> MagicMock:
    ch = MagicMock()
    ch.send = AsyncMock()
    ch.guild = MagicMock()
    return ch


# ---------------------------------------------------------------------------
# handle_cancel_button
# ---------------------------------------------------------------------------


@patch("bot.discord_ingest.service.remove_submission_dir")
async def test_cancel_button_deletes_submission(mock_remove, session, board):
    sub = make_submission(board, author_id=AUTHOR_ID, channel_id=100)
    session.add(sub)
    await session.flush()
    sub_id = sub.id

    dest = MockDest()
    outcome = await handle_cancel_button(session, _event(CURATOR_ID, sub_id), dest, _settings())

    remaining = await session.scalar(select(Submission).where(Submission.id == sub_id))
    assert remaining is None
    assert isinstance(outcome, Tombstone)
    assert dest.cleared_triggers == [(100, sub.source_discord_message_id)]  # trigger 🦋 removed
    assert dest.archived  # thread archived


@patch("bot.discord_ingest.service.remove_submission_dir")
async def test_cancel_button_op_can_cancel(mock_remove, session, board):
    """The OP (author) can cancel their own submission."""
    sub = make_submission(board, author_id=AUTHOR_ID, channel_id=100)
    session.add(sub)
    await session.flush()
    sub_id = sub.id

    outcome = await handle_cancel_button(session, _event(AUTHOR_ID, sub_id), MockDest(), _settings())

    remaining = await session.scalar(select(Submission).where(Submission.id == sub_id))
    assert remaining is None
    assert isinstance(outcome, Tombstone)


async def test_cancel_button_unauthorized_user_rejected(session, board):
    sub = make_submission(board, author_id=AUTHOR_ID, channel_id=100)
    session.add(sub)
    await session.flush()
    sub_id = sub.id

    outcome = await handle_cancel_button(session, _event(777, sub_id), MockDest(), _settings())

    remaining = await session.scalar(select(Submission).where(Submission.id == sub_id))
    assert remaining is not None
    assert isinstance(outcome, Ack)  # rejected, not tombstoned


async def test_cancel_button_published_submission_blocked(session, board):
    """Cannot cancel an already-published submission via button."""
    sub = make_submission(board, state=SubmissionState.PUBLISHED.value, author_id=AUTHOR_ID, channel_id=100)
    session.add(sub)
    await session.flush()

    attempt = PublishAttempt(submission_id=sub.id, success=True, bsky_url="https://bsky.app/x")
    session.add(attempt)
    await session.flush()

    outcome = await handle_cancel_button(session, _event(CURATOR_ID, sub.id), MockDest(), _settings())

    remaining = await session.scalar(select(Submission).where(Submission.id == sub.id))
    assert remaining is not None
    assert isinstance(outcome, Ack)


async def test_cancel_button_not_found(session, board):
    outcome = await handle_cancel_button(session, _event(CURATOR_ID, 99999), MockDest(), _settings())
    assert isinstance(outcome, Ack) and "not found" in outcome.message


# ---------------------------------------------------------------------------
# handle_confirm_button
# ---------------------------------------------------------------------------


@patch("bot.discord_ingest.service._auto_add_to_playlist", new_callable=AsyncMock, return_value=0)
@patch("bot.discord_ingest.service._playlist_close_ready", new_callable=AsyncMock, return_value=False)
async def test_confirm_button_queues_submission(mock_pclose, mock_playlist, session, board):
    sub = make_submission(
        board, state=SubmissionState.READY_TO_QUEUE.value, author_id=AUTHOR_ID,
        channel_id=100, source_waived=True,
    )
    session.add(sub)
    await session.flush()

    req = ConfirmationRequest(submission_id=sub.id, bot_message_id=8000)
    session.add(req)
    await session.flush()

    outcome = await handle_confirm_button(session, _event(CURATOR_ID, sub.id), MockDest(), _settings())

    assert sub.state == SubmissionState.QUEUED.value
    assert req.confirmed_at is not None
    assert req.confirmed_by == CURATOR_ID
    assert isinstance(outcome, Tombstone)


@patch("bot.discord_ingest.service.recompute_and_request", new_callable=AsyncMock)
async def test_confirm_button_refuses_when_gap_reopened(mock_recompute, session, board):
    """A gap that reopened after the Queue button was posted (e.g. a late-added image needing
    alt text) must block the click: no queue, an ephemeral refusal, and a recompute to refresh."""
    sub = make_submission(
        board, state=SubmissionState.READY_TO_QUEUE.value, author_id=AUTHOR_ID,
        channel_id=100, source_waived=True,
    )
    session.add(sub)
    await session.flush()
    session.add(Attachment(
        submission_id=sub.id, discord_attachment_id=1, filename="pic.jpg", discord_url="u",
        is_image=True, is_video=False, alt_text_status=AltTextStatus.NEEDED.value,
    ))
    req = ConfirmationRequest(submission_id=sub.id, bot_message_id=8050)
    session.add(req)
    await session.flush()

    outcome = await handle_confirm_button(session, _event(CURATOR_ID, sub.id), MockDest(), _settings())

    assert sub.state == SubmissionState.READY_TO_QUEUE.value  # not queued
    assert req.confirmed_at is None
    mock_recompute.assert_awaited_once()
    assert isinstance(outcome, Ack)
    assert "alt text" in outcome.message
    assert outcome.ephemeral is True


@patch("bot.discord_ingest.service._auto_add_to_playlist", new_callable=AsyncMock, return_value=0)
@patch("bot.discord_ingest.service._playlist_close_ready", new_callable=AsyncMock, return_value=False)
async def test_confirm_button_unauthorized_user_rejected(mock_pclose, mock_playlist, session, board):
    sub = make_submission(board, state=SubmissionState.READY_TO_QUEUE.value, author_id=AUTHOR_ID, channel_id=100)
    session.add(sub)
    await session.flush()

    req = ConfirmationRequest(submission_id=sub.id, bot_message_id=8001)
    session.add(req)
    await session.flush()

    outcome = await handle_confirm_button(session, _event(777, sub.id), MockDest(), _settings())

    assert sub.state == SubmissionState.READY_TO_QUEUE.value
    assert isinstance(outcome, Ack)


async def test_confirm_button_no_pending_req(session, board):
    """With no open ConfirmationRequest, returns 'Already queued' message."""
    sub = make_submission(board, state=SubmissionState.READY_TO_QUEUE.value, channel_id=100)
    session.add(sub)
    await session.flush()

    outcome = await handle_confirm_button(session, _event(CURATOR_ID, sub.id), MockDest(), _settings())

    assert isinstance(outcome, Ack) and "Already queued" in outcome.message


async def test_confirm_button_already_queued_blocked(session, board):
    sub = make_submission(board, state=SubmissionState.QUEUED.value, channel_id=100)
    session.add(sub)
    await session.flush()

    req = ConfirmationRequest(submission_id=sub.id, bot_message_id=8002)
    session.add(req)
    await session.flush()

    outcome = await handle_confirm_button(session, _event(CURATOR_ID, sub.id), MockDest(), _settings())

    assert isinstance(outcome, Ack) and "Nothing to queue" in outcome.message


# ---------------------------------------------------------------------------
# handle_label_reaction (graphic classification)
# ---------------------------------------------------------------------------


@patch("bot.discord_ingest.service.recompute_and_request", new_callable=AsyncMock)
async def test_label_reaction_graphic_yes(mock_recompute, session, board):
    """🩸 emoji sets graphic_status to GRAPHIC."""
    sub = make_submission(board, channel_id=100, author_id=AUTHOR_ID)
    session.add(sub)
    await session.flush()

    req = ContentLabelRequest(submission_id=sub.id, bot_message_id=3000)
    session.add(req)
    await session.flush()

    channel = _channel_mock()
    member = MagicMock()
    member.roles = []

    await handle_label_reaction(
        session,
        settings=_settings(),
        channel=channel,
        message_id=3000,
        emoji="🩸",
        member=member,
        user_id=CURATOR_ID,
    )

    assert sub.graphic_status == GraphicStatus.GRAPHIC.value
    assert req.answered_at is not None
    mock_recompute.assert_called_once()


@patch("bot.discord_ingest.service.recompute_and_request", new_callable=AsyncMock)
async def test_label_reaction_graphic_sets_answered_at(mock_recompute, session, board):
    """After a valid label reaction, req.answered_at and req.answered_by are set."""
    from bot.moderation import GRAPHIC_YES_EMOJI

    sub = make_submission(board, channel_id=100, author_id=AUTHOR_ID)
    session.add(sub)
    await session.flush()

    req = ContentLabelRequest(submission_id=sub.id, bot_message_id=3001)
    session.add(req)
    await session.flush()

    channel = _channel_mock()
    member = MagicMock()
    member.roles = []

    await handle_label_reaction(
        session,
        settings=_settings(),
        channel=channel,
        message_id=3001,
        emoji=GRAPHIC_YES_EMOJI,
        member=member,
        user_id=CURATOR_ID,
    )

    assert req.answered_at is not None
    assert req.answered_by == CURATOR_ID


@patch("bot.discord_ingest.service.recompute_and_request", new_callable=AsyncMock)
async def test_label_reaction_unknown_emoji_ignored(mock_recompute, session, board):
    """Unrecognised emoji does nothing."""
    sub = make_submission(board, channel_id=100, author_id=AUTHOR_ID)
    session.add(sub)
    await session.flush()

    req = ContentLabelRequest(submission_id=sub.id, bot_message_id=3002)
    session.add(req)
    await session.flush()

    channel = _channel_mock()
    await handle_label_reaction(
        session,
        settings=_settings(),
        channel=channel,
        message_id=3002,
        emoji="🐶",
        member=None,
        user_id=CURATOR_ID,
    )

    assert sub.graphic_status == GraphicStatus.UNKNOWN.value
    mock_recompute.assert_not_called()


@patch("bot.discord_ingest.service.recompute_and_request", new_callable=AsyncMock)
async def test_label_reaction_already_answered_ignored(mock_recompute, session, board):
    sub = make_submission(board, channel_id=100)
    session.add(sub)
    await session.flush()

    req = ContentLabelRequest(submission_id=sub.id, bot_message_id=3003)
    req.answered_at = datetime.now(timezone.utc)
    session.add(req)
    await session.flush()

    channel = _channel_mock()
    await handle_label_reaction(
        session,
        settings=_settings(),
        channel=channel,
        message_id=3003,
        emoji="🩸",
        member=None,
        user_id=CURATOR_ID,
    )

    mock_recompute.assert_not_called()


# ---------------------------------------------------------------------------
# handle_metadata_reaction
# ---------------------------------------------------------------------------


@patch("bot.discord_ingest.service.recompute_and_request", new_callable=AsyncMock)
async def test_metadata_reaction_confirms_link(mock_recompute, session, board):
    sub = make_submission(board, channel_id=100, author_id=AUTHOR_ID)
    session.add(sub)
    await session.flush()

    req = MetadataRequest(submission_id=sub.id, bot_message_id=4000)
    session.add(req)
    await session.flush()

    channel = _channel_mock()
    member = MagicMock()
    member.roles = []

    await handle_metadata_reaction(
        session,
        settings=_settings(),
        channel=channel,
        message_id=4000,
        member=member,
        user_id=CURATOR_ID,
    )

    assert req.answer == "confirmed"
    assert req.answered_at is not None
    mock_recompute.assert_called_once()


@patch("bot.discord_ingest.service.recompute_and_request", new_callable=AsyncMock)
async def test_metadata_reaction_unauthorized_ignored(mock_recompute, session, board):
    sub = make_submission(board, channel_id=100, author_id=AUTHOR_ID)
    session.add(sub)
    await session.flush()

    req = MetadataRequest(submission_id=sub.id, bot_message_id=4001)
    session.add(req)
    await session.flush()

    channel = _channel_mock()
    member = MagicMock()
    member.roles = []

    await handle_metadata_reaction(
        session,
        settings=_settings(),
        channel=channel,
        message_id=4001,
        member=member,
        user_id=777,  # not OP or curator
    )

    assert req.answer is None
    mock_recompute.assert_not_called()


# ---------------------------------------------------------------------------
# handle_confirmation_reaction
# ---------------------------------------------------------------------------


@patch("bot.discord_ingest.service._auto_add_to_playlist", new_callable=AsyncMock, return_value=0)
@patch("bot.discord_ingest.service._playlist_close_ready", new_callable=AsyncMock, return_value=False)
async def test_confirmation_reaction_queues_submission(mock_pclose, mock_playlist, session, board):
    sub = make_submission(
        board, state=SubmissionState.READY_TO_QUEUE.value, channel_id=100,
        author_id=AUTHOR_ID, source_waived=True,
    )
    session.add(sub)
    await session.flush()

    req = ConfirmationRequest(submission_id=sub.id, bot_message_id=7000)
    session.add(req)
    await session.flush()

    channel = _channel_mock()
    member = MagicMock()
    member.roles = []

    result = await handle_confirmation_reaction(
        session,
        settings=_settings(),
        channel=channel,
        message_id=7000,
        member=member,
        user_id=CURATOR_ID,
    )

    assert result is True
    assert sub.state == SubmissionState.QUEUED.value
    assert req.confirmed_by == CURATOR_ID


@patch("bot.discord_ingest.service.recompute_and_request", new_callable=AsyncMock)
async def test_confirmation_reaction_refuses_when_gap_reopened(mock_recompute, session, board):
    """The ✅ reaction path re-validates gaps too: a reopened gap blocks the queue."""
    sub = make_submission(
        board, state=SubmissionState.READY_TO_QUEUE.value, channel_id=100,
        author_id=AUTHOR_ID, source_waived=True,
    )
    session.add(sub)
    await session.flush()
    session.add(Attachment(
        submission_id=sub.id, discord_attachment_id=1, filename="pic.jpg", discord_url="u",
        is_image=True, is_video=False, alt_text_status=AltTextStatus.NEEDED.value,
    ))
    req = ConfirmationRequest(submission_id=sub.id, bot_message_id=7050)
    session.add(req)
    await session.flush()

    channel = _channel_mock()
    member = MagicMock()
    member.roles = []

    result = await handle_confirmation_reaction(
        session, settings=_settings(), channel=channel, message_id=7050,
        member=member, user_id=CURATOR_ID,
    )

    assert result is False
    assert sub.state == SubmissionState.READY_TO_QUEUE.value  # not queued
    assert req.confirmed_at is None
    mock_recompute.assert_awaited_once()
    channel.send.assert_awaited_once()
    assert "alt text" in channel.send.call_args.args[0]


@patch("bot.discord_ingest.service.recompute_and_request", new_callable=AsyncMock)
async def test_confirmation_reaction_refusal_notice_send_failure_swallowed(mock_recompute, session, board):
    """A failure posting the blocked-notice must not crash the refusal path."""
    sub = make_submission(
        board, state=SubmissionState.READY_TO_QUEUE.value, channel_id=100,
        author_id=AUTHOR_ID, source_waived=True,
    )
    session.add(sub)
    await session.flush()
    session.add(Attachment(
        submission_id=sub.id, discord_attachment_id=1, filename="pic.jpg", discord_url="u",
        is_image=True, is_video=False, alt_text_status=AltTextStatus.NEEDED.value,
    ))
    session.add(ConfirmationRequest(submission_id=sub.id, bot_message_id=7060))
    await session.flush()

    channel = _channel_mock()
    channel.send = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=500), "boom"))
    member = MagicMock()
    member.roles = []

    result = await handle_confirmation_reaction(
        session, settings=_settings(), channel=channel, message_id=7060,
        member=member, user_id=CURATOR_ID,
    )

    assert result is False
    assert sub.state == SubmissionState.READY_TO_QUEUE.value


@patch("bot.discord_ingest.service._auto_add_to_playlist", new_callable=AsyncMock, return_value=0)
@patch("bot.discord_ingest.service._playlist_close_ready", new_callable=AsyncMock, return_value=False)
async def test_confirmation_reaction_unauthorized_returns_false(mock_pclose, mock_playlist, session, board):
    sub = make_submission(board, state=SubmissionState.READY_TO_QUEUE.value, channel_id=100, author_id=AUTHOR_ID)
    session.add(sub)
    await session.flush()

    req = ConfirmationRequest(submission_id=sub.id, bot_message_id=7001)
    session.add(req)
    await session.flush()

    channel = _channel_mock()
    member = MagicMock()
    member.roles = []

    result = await handle_confirmation_reaction(
        session,
        settings=_settings(),
        channel=channel,
        message_id=7001,
        member=member,
        user_id=777,
    )

    assert result is False
    assert sub.state == SubmissionState.READY_TO_QUEUE.value


async def test_confirmation_reaction_no_req_returns_false(session, board):
    channel = _channel_mock()
    result = await handle_confirmation_reaction(
        session,
        settings=_settings(),
        channel=channel,
        message_id=99999,
        member=None,
        user_id=CURATOR_ID,
    )
    assert result is False


# ---------------------------------------------------------------------------
# handle_graphic_button
# ---------------------------------------------------------------------------


@patch("bot.discord_ingest.service.recompute_and_request", new_callable=AsyncMock)
async def test_graphic_button_marks_graphic(mock_recompute, session, board):
    sub = make_submission(board, channel_id=100, author_id=AUTHOR_ID)
    session.add(sub)
    await session.flush()

    req = ContentLabelRequest(submission_id=sub.id, bot_message_id=5000)
    session.add(req)
    await session.flush()

    outcome = await handle_graphic_button(session, _event(CURATOR_ID, sub.id), MockDest(), _settings())

    assert sub.graphic_status == GraphicStatus.GRAPHIC.value
    assert req.answered_at is not None
    mock_recompute.assert_called_once()
    assert isinstance(outcome, Tombstone)


@patch("bot.discord_ingest.service.recompute_and_request", new_callable=AsyncMock)
async def test_graphic_button_unauthorized_rejected(mock_recompute, session, board):
    sub = make_submission(board, channel_id=100, author_id=AUTHOR_ID)
    session.add(sub)
    await session.flush()

    req = ContentLabelRequest(submission_id=sub.id, bot_message_id=5001)
    session.add(req)
    await session.flush()

    outcome = await handle_graphic_button(session, _event(777, sub.id), MockDest(), _settings())

    assert sub.graphic_status == GraphicStatus.UNKNOWN.value
    assert isinstance(outcome, Ack)
    mock_recompute.assert_not_called()


async def test_graphic_button_no_req(session, board):
    """If no open ContentLabelRequest, returns 'Already classified' message."""
    sub = make_submission(board, channel_id=100)
    session.add(sub)
    await session.flush()

    outcome = await handle_graphic_button(session, _event(CURATOR_ID, sub.id), MockDest(), _settings())

    assert isinstance(outcome, Ack) and "Already classified" in outcome.message


# ---------------------------------------------------------------------------
# handle_playlist_skip_button
# ---------------------------------------------------------------------------


async def test_playlist_skip_sets_flag(session, board):
    sub = make_submission(board, channel_id=100, author_id=AUTHOR_ID)
    sub.playlist_skipped = False
    session.add(sub)
    await session.flush()

    outcome = await handle_playlist_skip_button(session, _event(AUTHOR_ID, sub.id), MockDest(), _settings())

    assert sub.playlist_skipped is True
    assert isinstance(outcome, Tombstone)


async def test_playlist_skip_already_opted_out(session, board):
    sub = make_submission(board, channel_id=100, author_id=AUTHOR_ID)
    sub.playlist_skipped = True
    session.add(sub)
    await session.flush()

    outcome = await handle_playlist_skip_button(session, _event(AUTHOR_ID, sub.id), MockDest(), _settings())

    assert isinstance(outcome, Ack) and "Already opted out" in outcome.message


async def test_playlist_skip_unauthorized_rejected(session, board):
    sub = make_submission(board, channel_id=100, author_id=AUTHOR_ID)
    sub.playlist_skipped = False
    session.add(sub)
    await session.flush()

    outcome = await handle_playlist_skip_button(session, _event(777, sub.id), MockDest(), _settings())

    assert sub.playlist_skipped is False
    assert isinstance(outcome, Ack)


# ---------------------------------------------------------------------------
# handle_edit_button
# ---------------------------------------------------------------------------


async def test_edit_button_sends_modal_for_authorized(session, board):
    sub = make_submission(board, channel_id=100, author_id=AUTHOR_ID)
    session.add(sub)
    await session.flush()

    outcome = await handle_edit_button(session, _event(AUTHOR_ID, sub.id), _settings())
    assert isinstance(outcome, OpenModal)


async def test_edit_button_unauthorized_rejected(session, board):
    sub = make_submission(board, channel_id=100, author_id=AUTHOR_ID)
    session.add(sub)
    await session.flush()

    outcome = await handle_edit_button(session, _event(777, sub.id), _settings())
    assert isinstance(outcome, Ack) and "not authorised" in outcome.message


async def test_edit_button_not_found(session, board):
    outcome = await handle_edit_button(session, _event(CURATOR_ID, 99999), _settings())
    assert isinstance(outcome, Ack) and "not found" in outcome.message


# ---------------------------------------------------------------------------
# apply_post_edits
# ---------------------------------------------------------------------------


async def test_apply_post_edits_updates_title(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()

    link = SubmissionLink(
        submission_id=sub.id,
        order_index=0,
        raw_url="https://example.com",
        canonical_url="https://example.com",
        domain_family="example",
        resolved_title="Old Title",
    )
    session.add(link)
    await session.flush()

    await apply_post_edits(session, submission_id=sub.id, new_title="New Title")
    assert link.resolved_title == "New Title"


async def test_apply_post_edits_strips_whitespace(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()

    link = SubmissionLink(
        submission_id=sub.id,
        order_index=0,
        raw_url="https://example.com",
        canonical_url="https://example.com",
        domain_family="example",
        resolved_title="Old Title",
    )
    session.add(link)
    await session.flush()

    await apply_post_edits(session, submission_id=sub.id, new_title="  Trimmed  ")
    assert link.resolved_title == "Trimmed"


async def test_apply_post_edits_empty_string_sets_none(session, board):
    """An empty/whitespace-only new title is stored as None."""
    sub = make_submission(board)
    session.add(sub)
    await session.flush()

    link = SubmissionLink(
        submission_id=sub.id,
        order_index=0,
        raw_url="https://example.com",
        canonical_url="https://example.com",
        domain_family="example",
        resolved_title="Some Title",
    )
    session.add(link)
    await session.flush()

    await apply_post_edits(session, submission_id=sub.id, new_title="   ")
    assert link.resolved_title is None


async def test_apply_post_edits_no_link_no_crash(session, board):
    """If there's no primary link, apply_post_edits returns without error."""
    sub = make_submission(board)
    session.add(sub)
    await session.flush()

    # Should not raise
    await apply_post_edits(session, submission_id=sub.id, new_title="Anything")


# ---------------------------------------------------------------------------
# handle_metadata_confirm_button
# ---------------------------------------------------------------------------


@patch("bot.discord_ingest.service.recompute_and_request", new_callable=AsyncMock)
async def test_metadata_confirm_button_happy_path(mock_recompute, session, board):
    """Curator clicks 'use link as-is': request marked confirmed, recompute runs."""
    sub = make_submission(board, channel_id=100, author_id=AUTHOR_ID)
    session.add(sub)
    await session.flush()

    req = MetadataRequest(submission_id=sub.id, bot_message_id=6000)
    session.add(req)
    await session.flush()

    dest = MockDest()
    outcome = await handle_metadata_confirm_button(session, _event(CURATOR_ID, sub.id), dest, _settings())

    assert req.answer == "confirmed"
    assert req.answered_by == CURATOR_ID
    assert req.answered_at is not None
    assert isinstance(outcome, Tombstone)  # button tombstoned
    assert dest.sent  # metadata_confirmed notice posted to the thread
    mock_recompute.assert_called_once()


@patch("bot.discord_ingest.service.recompute_and_request", new_callable=AsyncMock)
async def test_metadata_confirm_button_unauthorized_rejected(mock_recompute, session, board):
    sub = make_submission(board, channel_id=100, author_id=AUTHOR_ID)
    session.add(sub)
    await session.flush()

    req = MetadataRequest(submission_id=sub.id, bot_message_id=6001)
    session.add(req)
    await session.flush()

    outcome = await handle_metadata_confirm_button(session, _event(777, sub.id), MockDest(), _settings())

    assert req.answer is None
    assert req.answered_at is None
    assert isinstance(outcome, Ack)
    mock_recompute.assert_not_called()


@patch("bot.discord_ingest.service.recompute_and_request", new_callable=AsyncMock)
async def test_metadata_confirm_button_already_answered(mock_recompute, session, board):
    """No open MetadataRequest: replies 'Already confirmed.' and stops."""
    sub = make_submission(board, channel_id=100, author_id=AUTHOR_ID)
    session.add(sub)
    await session.flush()

    req = MetadataRequest(submission_id=sub.id, bot_message_id=6002)
    req.answer = "confirmed"
    req.answered_at = datetime.now(timezone.utc)
    session.add(req)
    await session.flush()

    outcome = await handle_metadata_confirm_button(session, _event(CURATOR_ID, sub.id), MockDest(), _settings())

    assert isinstance(outcome, Ack) and "Already confirmed" in outcome.message
    mock_recompute.assert_not_called()


@patch("bot.discord_ingest.service.recompute_and_request", new_callable=AsyncMock)
async def test_metadata_confirm_button_missing_submission(mock_recompute, session, board):
    """Open request whose submission row is gone: silent no-op."""
    req = MetadataRequest(submission_id=99999, bot_message_id=6003)
    session.add(req)
    await session.flush()

    outcome = await handle_metadata_confirm_button(session, _event(CURATOR_ID, 99999), MockDest(), _settings())

    assert req.answer is None
    assert isinstance(outcome, Noop)
    mock_recompute.assert_not_called()


# ---------------------------------------------------------------------------
# handle_graphic_button - uncovered branches
# ---------------------------------------------------------------------------


@patch("bot.discord_ingest.service.recompute_and_request", new_callable=AsyncMock)
async def test_graphic_button_missing_submission(mock_recompute, session, board):
    """Open request whose submission row is gone: silent no-op."""
    req = ContentLabelRequest(submission_id=99999, bot_message_id=5002)
    session.add(req)
    await session.flush()

    outcome = await handle_graphic_button(session, _event(CURATOR_ID, 99999), MockDest(), _settings())

    assert req.answered_at is None
    assert isinstance(outcome, Noop)
    mock_recompute.assert_not_called()


# ---------------------------------------------------------------------------
# handle_playlist_skip_button - uncovered branches
# ---------------------------------------------------------------------------


async def test_playlist_skip_submission_not_found(session, board):
    outcome = await handle_playlist_skip_button(session, _event(CURATOR_ID, 99999), MockDest(), _settings())
    assert isinstance(outcome, Ack) and "not found" in outcome.message


@patch("bot.discord_ingest.service._do_playlist_remove", new_callable=AsyncMock)
async def test_playlist_skip_removes_existing_playlist_adds(mock_remove, session, board):
    """Opting out after a successful playlist add removes the video."""
    sub = make_submission(board, channel_id=100, author_id=AUTHOR_ID)
    sub.playlist_skipped = False
    session.add(sub)
    await session.flush()

    row = YoutubePlaylistAdd(
        board_id=board.id,
        source_discord_message_id=sub.source_discord_message_id,
        video_id="dQw4w9WgXcQ",
        playlist_id="PL123",
        discord_requester_id=AUTHOR_ID,
        success=True,
    )
    session.add(row)
    await session.flush()

    dest = MockDest()
    outcome = await handle_playlist_skip_button(session, _event(AUTHOR_ID, sub.id), dest, _settings())

    assert sub.playlist_skipped is True
    assert isinstance(outcome, Tombstone)
    mock_remove.assert_called_once()
    assert mock_remove.call_args[0][0] is row
    assert mock_remove.call_args[0][1] is dest  # removal notices go through the Surface


async def test_playlist_skip_on_queued_schedules_archive(session, board):
    """Skipping the playlist on an already-queued submission re-arms the thread
    archive (through the Surface) with the remaining close delay."""
    sub = make_submission(board, state=SubmissionState.QUEUED.value, channel_id=100, author_id=AUTHOR_ID)
    sub.playlist_skipped = False
    sub.thread_id = 500
    session.add(sub)
    await session.flush()

    dest = MockDest()
    await handle_playlist_skip_button(session, _event(AUTHOR_ID, sub.id), dest, _settings())

    assert sub.playlist_skipped is True
    assert len(dest.archive_delays) == 1  # archive re-armed with an explicit delay
    assert isinstance(dest.archive_delays[0], float)
