"""Conformance tests for the Discord renderer (surface-agnostic core, issue #50).

The load-bearing contract: a component descriptor's `action_id` must survive verbatim
as the rendered widget's `custom_id`, so the existing on_interaction routing and every
persisted button keep working after descriptors replace the make_*_view factories.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import discord

from bot.curation.components import Button, ButtonStyle, ModalSpec, PreviewImage, Select, SelectOption, TextField
from bot.discord_ingest.render import (
    _dispatch_modal_submit,
    render_components,
    render_modal,
    render_preview,
)


def test_button_action_id_becomes_custom_id():
    view = render_components([Button(label="Queue for posting", action_id="confirm:42",
                                     style=ButtonStyle.SUCCESS, emoji="✅")])
    assert view is not None
    (btn,) = view.children
    assert isinstance(btn, discord.ui.Button)
    assert btn.custom_id == "confirm:42"
    assert btn.label == "Queue for posting"
    assert btn.style == discord.ButtonStyle.success


def test_disabled_button_is_disabled():
    # Tombstone buttons (make_disabled_view equivalent) are disabled; their custom_id
    # is irrelevant (discord.py auto-assigns one) since a disabled button can't route.
    view = render_components([Button(label="Queued ✅", action_id="", disabled=True)])
    (btn,) = view.children
    assert btn.disabled is True
    assert btn.label == "Queued ✅"


def test_select_action_id_and_options_round_trip():
    view = render_components([Select(
        action_id="alt_pick:7", placeholder="pick an image",
        options=[SelectOption(label="robot.jpg", value="11"),
                 SelectOption(label="clip.mp4", value="12")],
    )])
    (sel,) = view.children
    assert isinstance(sel, discord.ui.Select)
    assert sel.custom_id == "alt_pick:7"
    assert [(o.label, o.value) for o in sel.options] == [("robot.jpg", "11"), ("clip.mp4", "12")]


def test_empty_components_render_to_none():
    assert render_components(None) is None
    assert render_components([]) is None


def test_modal_field_action_ids_become_input_custom_ids():
    # A TextField with no placeholder renders a placeholder-less input (the `or None`
    # path); action_id -> custom_id, same contract as buttons.
    modal = render_modal(ModalSpec(
        title="Edit post", action_id="edit_post:42",
        fields=[TextField(label="Post text", action_id="caption", default="hi")],
    ))
    assert modal.custom_id == "edit_post:42"
    (inp,) = [c for c in modal.children if isinstance(c, discord.ui.TextInput)]
    assert inp.custom_id == "caption" and inp.default == "hi"
    assert inp.placeholder is None


async def test_dispatch_unknown_action_id_is_a_noop():
    # A modal whose action_id matches neither known prefix routes nowhere and must
    # not touch the interaction (defensive fallthrough).
    inter = MagicMock()
    inter.response = MagicMock(send_message=AsyncMock())
    await _dispatch_modal_submit("mystery:1", {"caption": "x"}, inter)
    inter.response.send_message.assert_not_awaited()


def test_render_preview_delegates_to_file_builder():
    sentinel = MagicMock()
    with patch("bot.discord_ingest.service._discord_file_for_attachment", return_value=sentinel) as f:
        out = render_preview(PreviewImage(local_path="/x.png", filename="x.png"))
    f.assert_called_once_with("/x.png", "x.png")
    assert out is sentinel
