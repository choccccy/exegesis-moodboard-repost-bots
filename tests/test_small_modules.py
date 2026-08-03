"""Coverage for small glue modules: prompts/render descriptor factories and stray branches.

The view factories carry the custom_id contracts that on_interaction routing depends on.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from bot.accessibility import AltTextStatus, initial_alt_text
from bot.discord_ingest import render
from bot.curation import prompts

from conftest import bound_session_scope


# ---------------------------------------------------------------------------
# prompts + render: descriptor factories carry the routing custom_ids
# ---------------------------------------------------------------------------


def test_view_factories_carry_routing_custom_ids():
    # The custom_id prefixes are the routing contract for on_interaction; they must
    # survive the descriptor -> discord.ui render step byte-for-byte.
    cases = [
        (prompts.metadata_confirm_components, "meta_ok:9"),
        (prompts.graphic_components, "graphic:9"),
        (prompts.playlist_skip_components, "pl_skip:9"),
    ]
    for factory, expected_id in cases:
        view = render.render_components(factory(9))
        buttons = [c for c in view.children if isinstance(c, discord.ui.Button)]
        assert len(buttons) == 1
        assert buttons[0].custom_id == expected_id
    modal = render.render_modal(prompts.post_edit_modal(submission_id=9, current_title=None))
    assert modal.custom_id == "edit_post:9"


# ---------------------------------------------------------------------------
# stray branches
# ---------------------------------------------------------------------------


def test_initial_alt_text_not_required_for_non_media():
    status, body = initial_alt_text(is_image=False, is_video=False, discord_description="ignored")
    assert status is AltTextStatus.NOT_REQUIRED
    assert body is None


def test_configure_logging_survives_unwritable_file_handler(tmp_path):
    from bot.logging_setup import configure_logging

    saved_handlers = logging.getLogger().handlers[:]
    saved_level = logging.getLogger().level
    try:
        with patch("bot.logging_setup.logging.FileHandler", side_effect=OSError("read-only volume")):
            configure_logging("INFO", str(tmp_path))
        # stdout handler still installed despite the file handler failing
        assert any(isinstance(h, logging.StreamHandler) for h in logging.getLogger().handlers)
    finally:
        for h in logging.getLogger().handlers[:]:
            if h not in saved_handlers:
                h.close()
        logging.getLogger().handlers = saved_handlers
        logging.getLogger().setLevel(saved_level)
