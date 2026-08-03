"""Tests for editing alt text after submission: the expanded edit modal (caption +
per-image alt), apply_post_edits / apply_single_alt, and the >4-image picker flow.
"""

from __future__ import annotations

import itertools
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from sqlalchemy import select

from bot.config import BoardConfig
from bot.discord_ingest import render, service
from bot.curation import prompts
from bot.curation.handlers import apply_post_edits, apply_single_alt, handle_alt_edit_button, handle_alt_pick, handle_edit_button
from bot.curation.events import InteractionEvent
from bot.curation.outcomes import Ack, Noop, OpenModal
from bot.models import Attachment, SubmissionLink
from bot.state import AltTextStatus, SubmissionState

from conftest import make_submission


def _event(user_id=999, submission_id=0, *, values=()):
    """A normalized button/select event. member=None matches the pre-migration path
    where a MagicMock user was never a real discord.Member (so only OP/curator-id
    authorization applied)."""
    return InteractionEvent(
        user_id=user_id, submission_id=submission_id, member=None, values=tuple(values)
    )

_ids = itertools.count(80_000)


def _settings(curator_ids=None):
    cfg = BoardConfig(
        name="robots", discord_guild_id=1, discord_channel_id=100,
        bluesky_handle="robots.exegesis.space", tags=[],
        curator_user_ids=curator_ids or [],
    )
    s = MagicMock()
    s.board_for_channel.return_value = cfg
    return s


def _interaction(user_id=999, *, values=None):
    inter = MagicMock(spec=discord.Interaction)
    inter.user = MagicMock()
    inter.user.id = user_id
    inter.user.roles = []
    inter.data = {"values": values} if values is not None else {}
    inter.response = MagicMock()
    inter.response.send_message = AsyncMock()
    inter.response.send_modal = AsyncMock()
    return inter


async def _img(session, sub, *, filename="pic.jpg", status=AltTextStatus.NEEDED.value,
               body=None, is_video=False):
    att = Attachment(
        submission_id=sub.id, discord_attachment_id=next(_ids), filename=filename,
        discord_url=f"https://cdn.discord.com/{filename}",
        is_image=not is_video, is_video=is_video,
        alt_text_status=status, alt_text_body=body,
    )
    session.add(att)
    await session.flush()
    return att


# --- apply_post_edits / apply_single_alt ------------------------------------


async def test_apply_post_edits_sets_and_clears_alt(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    session.add(SubmissionLink(
        submission_id=sub.id, order_index=0, raw_url="u", canonical_url="u", domain_family="other",
    ))
    a1 = await _img(session, sub, filename="a.jpg", status=AltTextStatus.NEEDED.value)
    a2 = await _img(session, sub, filename="b.jpg", status=AltTextStatus.PROVIDED.value, body="old")

    await apply_post_edits(
        session, submission_id=sub.id, new_title="Caption",
        alt_updates={a1.id: "a description", a2.id: "  "}, edited_by=999,
    )

    assert a1.alt_text_status == AltTextStatus.PROVIDED.value and a1.alt_text_body == "a description"
    assert a1.alt_text_author == 999
    # Cleared to blank -> SKIPPED (keeps the post queueable), body dropped.
    assert a2.alt_text_status == AltTextStatus.SKIPPED.value and a2.alt_text_body is None
    primary = await session.scalar(select(SubmissionLink).where(SubmissionLink.submission_id == sub.id))
    assert primary.resolved_title == "Caption"


async def test_apply_post_edits_ignores_foreign_attachment(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    other = make_submission(board, source_discord_message_id=2)
    session.add(other)
    await session.flush()
    foreign = await _img(session, other, filename="other.jpg")

    # Alt update keyed to an attachment on a different submission must be ignored.
    await apply_post_edits(
        session, submission_id=sub.id, new_title="x",
        alt_updates={foreign.id: "hijack"}, edited_by=999,
    )
    await session.refresh(foreign)
    assert foreign.alt_text_body is None


async def test_apply_single_alt(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    att = await _img(session, sub)
    await apply_single_alt(session, attachment_id=att.id, value="one image", edited_by=555)
    assert att.alt_text_status == AltTextStatus.PROVIDED.value
    assert att.alt_text_body == "one image"
    assert att.alt_text_author == 555


# --- PostEditModal ----------------------------------------------------------


def test_post_edit_modal_builds_caption_and_alt_fields():
    modal = render.render_modal(prompts.post_edit_modal(
        submission_id=7, current_title="Title",
        media=[(1, "a.jpg", "alt one"), (2, "b.jpg", None)],
    ))
    inputs = [c for c in modal.children if isinstance(c, discord.ui.TextInput)]
    assert inputs[0].custom_id == "caption" and inputs[0].default == "Title"
    alt_ids = {c.custom_id for c in inputs if c.custom_id.startswith("alt:")}
    assert alt_ids == {"alt:1", "alt:2"}
    assert next(c for c in inputs if c.custom_id == "alt:1").default == "alt one"


def test_post_edit_modal_caps_alt_fields_at_four():
    media = [(i, f"{i}.jpg", None) for i in range(1, 7)]  # 6 images
    modal = render.render_modal(prompts.post_edit_modal(submission_id=7, current_title=None, media=media))
    alt_inputs = [c for c in modal.children if isinstance(c, discord.ui.TextInput) and c.custom_id.startswith("alt:")]
    assert len(alt_inputs) == 4  # caption + 4 = Discord's 5-input limit


async def test_post_edit_modal_on_submit_applies(session, board):
    # The modal's on_submit collects its field values by action_id and hands them to
    # render._dispatch_modal_submit, which routes to service.apply_post_edits.
    from conftest import bound_session_scope
    inter = _interaction()
    with (
        patch("bot.db.session_scope", bound_session_scope(session)),
        patch("bot.curation.handlers.apply_post_edits", new_callable=AsyncMock) as apply,
    ):
        await render._dispatch_modal_submit(
            "edit_post:7", {"caption": "New", "alt:1": "the alt"}, inter
        )
    apply.assert_awaited_once()
    assert apply.await_args.kwargs["new_title"] == "New"
    assert apply.await_args.kwargs["alt_updates"] == {1: "the alt"}
    assert apply.await_args.kwargs["edited_by"] == 999
    inter.response.send_message.assert_awaited_once()


async def test_descriptor_modal_on_submit_collects_field_values():
    # on_submit reads the live input values by action_id before dispatching.
    modal = render.render_modal(prompts.alt_edit_modal(5, "a.jpg", "old"))
    modal._inputs = {"alt": MagicMock(value="new alt")}
    inter = _interaction()
    with patch("bot.discord_ingest.render._dispatch_modal_submit", new_callable=AsyncMock) as disp:
        await modal.on_submit(inter)
    disp.assert_awaited_once_with("edit_alt:5", {"alt": "new alt"}, inter)


# --- handle_edit_button passes media ----------------------------------------


async def test_handle_edit_button_passes_first_four_media(session, board):
    sub = make_submission(board, state=SubmissionState.READY_TO_QUEUE.value, author_id=999)
    session.add(sub)
    await session.flush()
    session.add(SubmissionLink(
        submission_id=sub.id, order_index=0, raw_url="u", canonical_url="u", domain_family="other",
        resolved_title="Title",
    ))
    for i in range(6):
        await _img(session, sub, filename=f"{i}.jpg")

    outcome = await handle_edit_button(session, _event(999, sub.id), _settings())
    assert isinstance(outcome, OpenModal)
    alt_fields = [f for f in outcome.spec.fields if f.action_id.startswith("alt:")]
    assert len(alt_fields) == 4  # capped at 4


# --- make_confirm_view alt button gating ------------------------------------


def test_confirm_view_alt_button_only_when_over_four():
    def alt_button_present(view):
        return any(
            isinstance(c, discord.ui.Button) and (c.custom_id or "").startswith("alt_edit:")
            for c in view.children
        )
    assert not alt_button_present(render.render_components(prompts.confirm_components(1, media_count=4)))
    assert alt_button_present(render.render_components(prompts.confirm_components(1, media_count=5)))


# --- picker flow ------------------------------------------------------------


async def test_alt_edit_button_sends_picker(session, board):
    sub = make_submission(board, author_id=999)
    session.add(sub)
    await session.flush()
    await _img(session, sub, filename="a.jpg")
    await _img(session, sub, filename="b.mp4", is_video=True)

    outcome = await handle_alt_edit_button(session, _event(999, sub.id), _settings())
    assert isinstance(outcome, Ack)
    selects = list(outcome.components)
    assert len(selects) == 1
    assert {o.label for o in selects[0].options} == {"a.jpg", "b.mp4"}


def test_alt_overwritten_message_variants():
    from bot.curation import replies
    with_prev = replies.alt_text_overwritten("a.jpg", "the old alt")
    assert "a.jpg" in with_prev and "the old alt" in with_prev
    no_prev = replies.alt_text_overwritten("a.jpg", None)
    assert "a.jpg" in no_prev and "was:" not in no_prev
    long_prev = replies.alt_text_overwritten("a.jpg", "x" * 200)
    assert "..." in long_prev  # previous value truncated


async def test_alt_edit_button_submission_missing(session, board):
    outcome = await handle_alt_edit_button(session, _event(999, 999999), _settings())
    assert isinstance(outcome, Ack) and "not found" in outcome.message


async def test_alt_edit_button_over_25_media_still_sends(session, board):
    sub = make_submission(board, author_id=999)
    session.add(sub)
    await session.flush()
    for i in range(26):
        await _img(session, sub, filename=f"{i}.jpg")
    outcome = await handle_alt_edit_button(session, _event(999, sub.id), _settings())
    (select,) = outcome.components
    assert len(select.options) == 25  # capped at Discord's limit


async def test_alt_edit_button_no_media(session, board):
    sub = make_submission(board, author_id=999)
    session.add(sub)
    await session.flush()
    outcome = await handle_alt_edit_button(session, _event(999, sub.id), _settings())
    assert isinstance(outcome, Ack) and "no images" in outcome.message


async def test_alt_edit_button_unauthorized(session, board):
    sub = make_submission(board, author_id=1)
    session.add(sub)
    await session.flush()
    await _img(session, sub)
    outcome = await handle_alt_edit_button(session, _event(555, sub.id), _settings())
    assert isinstance(outcome, Ack) and "not authorised" in outcome.message


async def test_alt_pick_opens_modal(session, board):
    sub = make_submission(board, author_id=999)
    session.add(sub)
    await session.flush()
    att = await _img(session, sub, filename="a.jpg", body="cur")
    outcome = await handle_alt_pick(session, _event(999, sub.id, values=(str(att.id),)), _settings())
    assert isinstance(outcome, OpenModal)
    assert outcome.spec.action_id == f"edit_alt:{att.id}"


async def test_alt_pick_no_values_noop(session, board):
    sub = make_submission(board, author_id=999)
    session.add(sub)
    await session.flush()
    outcome = await handle_alt_pick(session, _event(999, sub.id, values=()), _settings())
    assert isinstance(outcome, Noop)


async def test_alt_pick_foreign_attachment_rejected(session, board):
    sub = make_submission(board, author_id=999)
    other = make_submission(board, source_discord_message_id=2, author_id=999)
    session.add_all([sub, other])
    await session.flush()
    foreign = await _img(session, other, filename="x.jpg")
    outcome = await handle_alt_pick(session, _event(999, sub.id, values=(str(foreign.id),)), _settings())
    assert isinstance(outcome, Ack) and "not found" in outcome.message


async def test_alt_pick_unauthorized(session, board):
    sub = make_submission(board, author_id=1)
    session.add(sub)
    await session.flush()
    att = await _img(session, sub)
    outcome = await handle_alt_pick(session, _event(555, sub.id, values=(str(att.id),)), _settings())
    assert isinstance(outcome, Ack) and "not authorised" in outcome.message


async def test_alt_edit_modal_on_submit_applies(session, board):
    from conftest import bound_session_scope
    inter = _interaction()
    with (
        patch("bot.db.session_scope", bound_session_scope(session)),
        patch("bot.curation.handlers.apply_single_alt", new_callable=AsyncMock) as apply,
    ):
        await render._dispatch_modal_submit("edit_alt:5", {"alt": "brand new alt"}, inter)
    apply.assert_awaited_once()
    assert apply.await_args.kwargs["attachment_id"] == 5
    assert apply.await_args.kwargs["value"] == "brand new alt"
    inter.response.send_message.assert_awaited_once()
