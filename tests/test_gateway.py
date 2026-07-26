"""Tests for the Discord interaction gateway (surface-agnostic core, Phase C):
`to_event` normalizes an inbound interaction, and `perform` carries out a handler's
HandlerOutcome against the live interaction.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord

from bot.curation.components import Button, ModalSpec, TextField
from bot.discord_ingest.gateway import perform, to_event
from bot.curation.outcomes import Ack, Noop, OpenModal, Tombstone


def _interaction(*, is_done=False):
    inter = MagicMock(spec=discord.Interaction)
    inter.response = MagicMock()
    inter.response.is_done = MagicMock(return_value=is_done)
    inter.response.send_message = AsyncMock()
    inter.response.send_modal = AsyncMock()
    inter.followup = MagicMock()
    inter.followup.send = AsyncMock()
    inter.message = MagicMock()
    inter.message.id = 123
    inter.message.edit = AsyncMock()
    return inter


# --- to_event ---------------------------------------------------------------

def test_to_event_carries_member_and_values():
    inter = MagicMock(spec=discord.Interaction)
    inter.user = MagicMock(spec=discord.Member)
    inter.user.id = 42
    inter.data = {"values": ["11", "12"]}
    event = to_event(inter, submission_id=7)
    assert event.user_id == 42
    assert event.submission_id == 7
    assert event.member is inter.user  # a real Member is carried through for authz
    assert event.values == ("11", "12")


def test_to_event_non_member_actor_has_no_member_and_no_values():
    inter = MagicMock(spec=discord.Interaction)
    inter.user = MagicMock(spec=discord.User)  # not a guild Member
    inter.user.id = 99
    inter.data = {}
    event = to_event(inter, submission_id=3)
    assert event.member is None
    assert event.values == ()


# --- perform ----------------------------------------------------------------

async def test_perform_noop_touches_nothing():
    inter = _interaction()
    await perform(inter, Noop())
    inter.response.send_message.assert_not_awaited()
    inter.response.send_modal.assert_not_awaited()
    inter.followup.send.assert_not_awaited()


async def test_perform_open_modal_sends_rendered_modal():
    inter = _interaction()
    spec = ModalSpec(title="Edit", action_id="edit_alt:5",
                     fields=[TextField(label="alt", action_id="alt")])
    await perform(inter, OpenModal(spec))
    inter.response.send_modal.assert_awaited_once()
    modal = inter.response.send_modal.await_args.args[0]
    assert isinstance(modal, discord.ui.Modal)
    assert modal.custom_id == "edit_alt:5"


async def test_perform_ack_uses_response_when_not_deferred():
    inter = _interaction(is_done=False)
    await perform(inter, Ack("Nope.", ephemeral=True))
    inter.response.send_message.assert_awaited_once()
    assert inter.response.send_message.await_args.args[0] == "Nope."
    assert inter.response.send_message.await_args.kwargs["ephemeral"] is True
    inter.followup.send.assert_not_awaited()


async def test_perform_ack_uses_followup_after_defer():
    inter = _interaction(is_done=True)
    await perform(inter, Ack("Done."))
    inter.followup.send.assert_awaited_once()
    inter.response.send_message.assert_not_awaited()


async def test_perform_ack_with_components_passes_a_view():
    inter = _interaction(is_done=False)
    await perform(inter, Ack("Pick one:", components=[Button(label="x", action_id="alt_pick:1")]))
    view = inter.response.send_message.await_args.kwargs["view"]
    assert isinstance(view, discord.ui.View)


async def test_perform_ack_without_components_passes_no_view():
    inter = _interaction(is_done=False)
    await perform(inter, Ack("Plain."))
    assert "view" not in inter.response.send_message.await_args.kwargs


async def test_perform_tombstone_disables_clicked_message():
    inter = _interaction()
    await perform(inter, Tombstone("Queued ✅"))
    inter.message.edit.assert_awaited_once()
    view = inter.message.edit.await_args.kwargs["view"]
    (btn,) = view.children
    assert btn.disabled is True and btn.label == "Queued ✅"


async def test_perform_tombstone_swallows_edit_failure():
    # The state change already committed; a failed tombstone edit must not raise.
    inter = _interaction()
    inter.message.edit = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "gone"))
    await perform(inter, Tombstone("Queued ✅"))  # must not raise
