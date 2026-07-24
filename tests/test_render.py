"""Conformance tests for the Discord renderer (surface-agnostic core, issue #50).

The load-bearing contract: a component descriptor's `action_id` must survive verbatim
as the rendered widget's `custom_id`, so the existing on_interaction routing and every
persisted button keep working after descriptors replace the make_*_view factories.
"""

from __future__ import annotations

import discord

from bot.components import Button, ButtonStyle, Select, SelectOption
from bot.discord_ingest.render import render_components


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
