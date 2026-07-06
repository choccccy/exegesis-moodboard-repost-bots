"""Coverage for small glue modules: DiscordNotifier, views, and stray branches.

These are thin but load-bearing: DiscordNotifier is the bridge between the
service layer and Discord threads, and the view factories carry the custom_id
contracts that on_interaction routing depends on.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from bot.accessibility import AltTextStatus, initial_alt_text
from bot.discord_ingest.discord_notifier import DiscordNotifier
from bot.discord_ingest import views

from conftest import bound_session_scope


# ---------------------------------------------------------------------------
# DiscordNotifier
# ---------------------------------------------------------------------------


async def test_notifier_send_passes_through():
    channel = MagicMock()
    channel.send = AsyncMock(return_value=MagicMock(id=7))
    notifier = DiscordNotifier(channel)

    msg = await notifier.send("hello", suppress_embeds=True)

    channel.send.assert_awaited_once_with("hello", suppress_embeds=True)
    assert msg.id == 7


async def test_notifier_archive_schedules_for_thread():
    thread = MagicMock(spec=discord.Thread)
    notifier = DiscordNotifier(thread)

    with patch("bot.discord_ingest.service._archive_thread_after_delay") as mock_archive:
        await notifier.archive("done")

    mock_archive.assert_called_once_with(thread, notice="done")


async def test_notifier_archive_noop_for_plain_channel():
    channel = MagicMock()  # not a discord.Thread
    notifier = DiscordNotifier(channel)

    with patch("bot.discord_ingest.service._archive_thread_after_delay") as mock_archive:
        await notifier.archive("done")

    mock_archive.assert_not_called()


# ---------------------------------------------------------------------------
# views: modal submit and button factories
# ---------------------------------------------------------------------------


async def test_post_edit_modal_on_submit_applies_edits(session):
    modal = views.PostEditModal(submission_id=42, current_title="old title")
    interaction = MagicMock(spec=discord.Interaction)
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    modal.caption_input = MagicMock()
    modal.caption_input.value = "new title"

    with patch("bot.db.session_scope", bound_session_scope(session)), \
         patch("bot.discord_ingest.service.apply_post_edits", new_callable=AsyncMock) as mock_apply:
        await modal.on_submit(interaction)

    mock_apply.assert_awaited_once()
    assert mock_apply.await_args.kwargs["submission_id"] == 42
    assert mock_apply.await_args.kwargs["new_title"] == "new title"
    interaction.response.send_message.assert_awaited_once()


def test_view_factories_carry_routing_custom_ids():
    # The custom_id prefixes are the routing contract for on_interaction.
    cases = [
        (views.make_metadata_confirm_view, "meta_ok:9"),
        (views.make_graphic_view, "graphic:9"),
        (views.make_playlist_skip_view, "pl_skip:9"),
    ]
    for factory, expected_id in cases:
        view = factory(9)
        buttons = [c for c in view.children if isinstance(c, discord.ui.Button)]
        assert len(buttons) == 1
        assert buttons[0].custom_id == expected_id
    assert views.PostEditModal(submission_id=9, current_title=None).custom_id == "edit_post:9"


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
