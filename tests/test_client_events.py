"""Tests for RepostBot event handlers: interactions, raw reactions, messages.

Covers on_interaction (component filter, edit-modal routing, expired-defer
fallback, custom_id prefix routing to service handlers), on_raw_reaction_add
(own-reaction guard, trigger routing, watched-channel gating, emoji routing
for graphic/metadata/confirmation/opt-out, cancel flows on source posts and
thread messages), on_raw_reaction_remove, and on_message reply gating.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import discord
import pytest

from bot.discord_ingest.client import RepostBot
from bot.discord_ingest.replies import (
    CONFIRMATION_EMOJI,
    METADATA_CONFIRM_EMOJI,
    PLAYLIST_OPT_OUT_EMOJI,
)
from bot.moderation import GRAPHIC_YES_EMOJI
from conftest import (
    bound_session_scope,
    make_interaction,
    make_reaction_payload,
)


def _not_found() -> discord.NotFound:
    return discord.NotFound(MagicMock(status=404, reason="Not Found"), "gone")


def _patch_user(uid: int = 42):
    """Patch the read-only RepostBot.user property to a stub with an id."""
    return patch.object(
        RepostBot, "user", new_callable=PropertyMock, return_value=SimpleNamespace(id=uid)
    )


# ---------------------------------------------------------------------------
# on_interaction
# ---------------------------------------------------------------------------


async def test_on_interaction_ignores_non_component(repost_bot):
    interaction = make_interaction()
    interaction.type = discord.InteractionType.application_command
    with patch("bot.discord_ingest.client.service.handle_confirm_button", new_callable=AsyncMock) as handler:
        await repost_bot.on_interaction(interaction)
    handler.assert_not_awaited()
    interaction.response.defer.assert_not_awaited()


async def test_on_interaction_edit_prefix_routes_without_defer(repost_bot, session):
    # The edit button's first response is the modal itself, so defer() must not
    # run; the client hands off to handle_edit_button immediately.
    interaction = make_interaction(custom_id="edit:7")
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch("bot.discord_ingest.client.service.handle_edit_button", new_callable=AsyncMock) as handler,
    ):
        await repost_bot.on_interaction(interaction)
    handler.assert_awaited_once()
    assert handler.await_args.args[2] == 7
    interaction.response.defer.assert_not_awaited()


@pytest.mark.parametrize(
    ("custom_id", "handler_name", "expected_id"),
    [
        ("alt_edit:8", "handle_alt_edit_button", 8),
        ("alt_pick:9", "handle_alt_pick", 9),
    ],
)
async def test_on_interaction_alt_prefixes_route_without_defer(
    repost_bot, session, custom_id, handler_name, expected_id
):
    # Both produce an initial response (a select message, then a modal), so neither defers.
    interaction = make_interaction(custom_id=custom_id)
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch(f"bot.discord_ingest.client.service.{handler_name}", new_callable=AsyncMock) as handler,
    ):
        await repost_bot.on_interaction(interaction)
    handler.assert_awaited_once()
    assert handler.await_args.args[2] == expected_id
    interaction.response.defer.assert_not_awaited()


async def test_on_interaction_expired_defer_falls_back_to_channel_message(repost_bot):
    interaction = make_interaction(custom_id="confirm:3")
    interaction.response.defer = AsyncMock(side_effect=_not_found())
    interaction.user.mention = "<@999>"
    with patch("bot.discord_ingest.client.service.handle_confirm_button", new_callable=AsyncMock) as handler:
        await repost_bot.on_interaction(interaction)
    handler.assert_not_awaited()
    interaction.channel.send.assert_awaited_once()
    assert "<@999>" in interaction.channel.send.await_args.args[0]


async def test_on_interaction_expired_defer_swallows_fallback_send_failure(repost_bot):
    interaction = make_interaction(custom_id="confirm:3")
    interaction.response.defer = AsyncMock(side_effect=_not_found())
    interaction.channel.send = AsyncMock(side_effect=RuntimeError("also down"))
    await repost_bot.on_interaction(interaction)  # must not raise


async def test_on_interaction_expired_defer_without_channel(repost_bot):
    interaction = make_interaction(custom_id="confirm:3")
    interaction.response.defer = AsyncMock(side_effect=_not_found())
    interaction.channel = None
    await repost_bot.on_interaction(interaction)  # must not raise


@pytest.mark.parametrize(
    ("custom_id", "handler_name", "expected_id"),
    [
        ("cancel:11", "handle_cancel_button", 11),
        ("confirm:12", "handle_confirm_button", 12),
        ("meta_ok:13", "handle_metadata_confirm_button", 13),
        ("graphic:14", "handle_graphic_button", 14),
        ("pl_skip:15", "handle_playlist_skip_button", 15),
        ("alt_skip:16", "handle_alt_skip_button", 16),
        ("no_source:17", "handle_no_source_button", 17),
    ],
)
async def test_on_interaction_routes_prefix_to_handler(
    repost_bot, session, custom_id, handler_name, expected_id
):
    interaction = make_interaction(custom_id=custom_id)
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch(f"bot.discord_ingest.client.service.{handler_name}", new_callable=AsyncMock) as handler,
    ):
        await repost_bot.on_interaction(interaction)
    interaction.response.defer.assert_awaited_once()
    handler.assert_awaited_once()
    assert handler.await_args.args[2] == expected_id


async def test_on_interaction_unknown_custom_id_is_ignored(repost_bot, session):
    interaction = make_interaction(custom_id="mystery:99")
    handler_names = [
        "handle_cancel_button",
        "handle_confirm_button",
        "handle_metadata_confirm_button",
        "handle_graphic_button",
        "handle_playlist_skip_button",
    ]
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch.multiple(
            "bot.discord_ingest.client.service",
            **{name: AsyncMock() for name in handler_names},
        ),
    ):
        import bot.discord_ingest.service as service_mod

        await repost_bot.on_interaction(interaction)
        for name in handler_names:
            getattr(service_mod, name).assert_not_awaited()
    interaction.response.defer.assert_awaited_once()


# ---------------------------------------------------------------------------
# on_raw_reaction_add
# ---------------------------------------------------------------------------


async def test_reaction_add_ignores_bots_own_reaction(repost_bot):
    payload = make_reaction_payload(user_id=42)
    repost_bot._fetch_message = AsyncMock()
    with (
        _patch_user(42),
        patch("bot.discord_ingest.client.service.handle_reaction", new_callable=AsyncMock) as handler,
    ):
        await repost_bot.on_raw_reaction_add(payload)
    repost_bot._fetch_message.assert_not_awaited()
    handler.assert_not_awaited()


async def test_reaction_add_trigger_on_watched_channel_calls_handle_reaction(repost_bot, session):
    payload = make_reaction_payload(channel_id=100)
    message = MagicMock()
    repost_bot._fetch_message = AsyncMock(return_value=message)
    repost_bot._http = MagicMock()
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch("bot.discord_ingest.client.service.handle_reaction", new_callable=AsyncMock) as handler,
    ):
        await repost_bot.on_raw_reaction_add(payload)
    handler.assert_awaited_once()
    assert handler.await_args.kwargs["message"] is message
    assert handler.await_args.kwargs["user_id"] == payload.user_id


async def test_reaction_add_trigger_on_unwatched_channel_ignored(repost_bot):
    payload = make_reaction_payload(channel_id=999)
    repost_bot._fetch_message = AsyncMock()
    await repost_bot.on_raw_reaction_add(payload)
    repost_bot._fetch_message.assert_not_awaited()


async def test_reaction_add_trigger_message_fetch_failure_skips(repost_bot):
    payload = make_reaction_payload(channel_id=100)
    repost_bot._fetch_message = AsyncMock(return_value=None)
    with patch("bot.discord_ingest.client.service.handle_reaction", new_callable=AsyncMock) as handler:
        await repost_bot.on_raw_reaction_add(payload)
    handler.assert_not_awaited()


async def test_reaction_add_trigger_handler_exception_is_swallowed(repost_bot, session):
    payload = make_reaction_payload(channel_id=100)
    repost_bot._fetch_message = AsyncMock(return_value=MagicMock())
    repost_bot._http = MagicMock()
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch(
            "bot.discord_ingest.client.service.handle_reaction",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ),
    ):
        await repost_bot.on_raw_reaction_add(payload)  # must not raise


@pytest.mark.parametrize(
    ("emoji", "handler_name"),
    [
        (GRAPHIC_YES_EMOJI, "handle_label_reaction"),
        (METADATA_CONFIRM_EMOJI, "handle_metadata_reaction"),
        (CONFIRMATION_EMOJI, "handle_confirmation_reaction"),
        (PLAYLIST_OPT_OUT_EMOJI, "handle_playlist_opt_out"),
    ],
)
async def test_reaction_add_routes_special_emoji(repost_bot, session, emoji, handler_name):
    payload = make_reaction_payload(emoji=emoji, channel_id=200)
    channel = MagicMock()
    repost_bot._resolve_channel = AsyncMock(return_value=channel)
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch(f"bot.discord_ingest.client.service.{handler_name}", new_callable=AsyncMock) as handler,
    ):
        await repost_bot.on_raw_reaction_add(payload)
    handler.assert_awaited_once()
    assert handler.await_args.kwargs["message_id"] == payload.message_id


@pytest.mark.parametrize(
    ("emoji", "handler_name"),
    [
        (GRAPHIC_YES_EMOJI, "handle_label_reaction"),
        (METADATA_CONFIRM_EMOJI, "handle_metadata_reaction"),
        (CONFIRMATION_EMOJI, "handle_confirmation_reaction"),
        (PLAYLIST_OPT_OUT_EMOJI, "handle_playlist_opt_out"),
        ("\N{CROSS MARK}", "handle_source_cancel_reaction"),
    ],
)
async def test_reaction_add_special_emoji_unresolvable_channel_skips(
    repost_bot, emoji, handler_name
):
    payload = make_reaction_payload(emoji=emoji, channel_id=200)
    repost_bot._resolve_channel = AsyncMock(return_value=None)
    with patch(f"bot.discord_ingest.client.service.{handler_name}", new_callable=AsyncMock) as handler:
        await repost_bot.on_raw_reaction_add(payload)
    handler.assert_not_awaited()


async def test_reaction_add_cancel_on_watched_channel_cancels_and_archives(repost_bot, session):
    # ❌ on the source post: submission cancelled, thread notified and archived,
    # plus a removed-video notice.
    payload = make_reaction_payload(emoji="\N{CROSS MARK}", channel_id=100)
    source_channel = MagicMock()
    thread = MagicMock(spec=discord.Thread)
    repost_bot._resolve_channel = AsyncMock(side_effect=[source_channel, thread])
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch(
            "bot.discord_ingest.client.service.handle_source_cancel_reaction",
            new_callable=AsyncMock,
            return_value=(555, True, ["vid123"]),
        ) as handler,
        patch("bot.discord_ingest.client.service._archive_thread", new_callable=AsyncMock) as archive,
    ):
        await repost_bot.on_raw_reaction_add(payload)
    handler.assert_awaited_once()
    archive.assert_awaited_once()
    sent = [call.args[0] for call in thread.send.await_args_list]
    assert any("cancel" in text.lower() for text in sent)
    assert any("vid123" in text for text in sent)


async def test_reaction_add_cancel_on_watched_channel_without_thread(repost_bot, session):
    # Nothing was cancelled and no thread exists: no notifications go out.
    payload = make_reaction_payload(emoji="\N{CROSS MARK}", channel_id=100)
    source_channel = MagicMock()
    repost_bot._resolve_channel = AsyncMock(return_value=source_channel)
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch(
            "bot.discord_ingest.client.service.handle_source_cancel_reaction",
            new_callable=AsyncMock, return_value=(None, False, []),
        ),
        patch("bot.discord_ingest.client.service._archive_thread", new_callable=AsyncMock) as archive,
    ):
        await repost_bot.on_raw_reaction_add(payload)
    archive.assert_not_awaited()
    assert repost_bot._resolve_channel.await_count == 1  # no thread lookup


async def test_reaction_add_cancel_watched_thread_unresolvable(repost_bot, session):
    # The submission had a thread but it can no longer be resolved: nothing sent.
    payload = make_reaction_payload(emoji="\N{CROSS MARK}", channel_id=100)
    repost_bot._resolve_channel = AsyncMock(side_effect=[MagicMock(), None])
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch(
            "bot.discord_ingest.client.service.handle_source_cancel_reaction",
            new_callable=AsyncMock, return_value=(555, True, ["vid123"]),
        ),
        patch("bot.discord_ingest.client.service._archive_thread", new_callable=AsyncMock) as archive,
    ):
        await repost_bot.on_raw_reaction_add(payload)
    archive.assert_not_awaited()


async def test_reaction_add_cancel_watched_non_thread_destination_skips_archive(repost_bot, session):
    # Destination resolves to a plain channel, not a Thread: confirmation and
    # video notices still go out, but no archive happens.
    payload = make_reaction_payload(emoji="\N{CROSS MARK}", channel_id=100)
    plain_channel = MagicMock()
    plain_channel.send = AsyncMock()
    repost_bot._resolve_channel = AsyncMock(side_effect=[MagicMock(), plain_channel])
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch(
            "bot.discord_ingest.client.service.handle_source_cancel_reaction",
            new_callable=AsyncMock, return_value=(555, True, ["vid123"]),
        ),
        patch("bot.discord_ingest.client.service._archive_thread", new_callable=AsyncMock) as archive,
    ):
        await repost_bot.on_raw_reaction_add(payload)
    archive.assert_not_awaited()
    assert plain_channel.send.await_count == 2


async def test_reaction_add_cancel_watched_video_removal_only(repost_bot, session):
    # Nothing cancelled, but a playlist video was removed: only that notice goes out.
    payload = make_reaction_payload(emoji="\N{CROSS MARK}", channel_id=100)
    thread = MagicMock(spec=discord.Thread)
    repost_bot._resolve_channel = AsyncMock(side_effect=[MagicMock(), thread])
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch(
            "bot.discord_ingest.client.service.handle_source_cancel_reaction",
            new_callable=AsyncMock, return_value=(555, False, ["vid123"]),
        ),
        patch("bot.discord_ingest.client.service._archive_thread", new_callable=AsyncMock) as archive,
    ):
        await repost_bot.on_raw_reaction_add(payload)
    archive.assert_not_awaited()
    thread.send.assert_awaited_once()
    assert "vid123" in thread.send.await_args.args[0]


async def test_reaction_add_cancel_in_thread_without_source_info(repost_bot, session):
    payload = make_reaction_payload(emoji="\N{CROSS MARK}", channel_id=555)
    repost_bot._resolve_channel = AsyncMock(return_value=MagicMock())
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch(
            "bot.discord_ingest.client.service.handle_cancel_reaction",
            new_callable=AsyncMock, return_value=None,
        ),
        patch("bot.discord_ingest.client.service._clear_trigger_reaction", new_callable=AsyncMock) as clear,
    ):
        await repost_bot.on_raw_reaction_add(payload)
    clear.assert_not_awaited()


async def test_reaction_add_cancel_in_thread_source_channel_unresolvable(repost_bot, session):
    payload = make_reaction_payload(emoji="\N{CROSS MARK}", channel_id=555)
    repost_bot._resolve_channel = AsyncMock(side_effect=[MagicMock(), None])
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch(
            "bot.discord_ingest.client.service.handle_cancel_reaction",
            new_callable=AsyncMock, return_value=(100, 777),
        ),
        patch("bot.discord_ingest.client.service._clear_trigger_reaction", new_callable=AsyncMock) as clear,
    ):
        await repost_bot.on_raw_reaction_add(payload)
    clear.assert_not_awaited()


async def test_reaction_add_cancel_in_thread_clears_source_trigger(repost_bot, session):
    # ❌ on a bot message inside a thread (unwatched channel id): the cancel
    # handler returns the source location and the trigger reaction gets cleared.
    payload = make_reaction_payload(emoji="\N{CROSS MARK}", channel_id=555)
    thread = MagicMock()
    src_channel = MagicMock()
    repost_bot._resolve_channel = AsyncMock(side_effect=[thread, src_channel])
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch(
            "bot.discord_ingest.client.service.handle_cancel_reaction",
            new_callable=AsyncMock,
            return_value=(100, 777),
        ),
        patch("bot.discord_ingest.client.service._clear_trigger_reaction", new_callable=AsyncMock) as clear,
    ):
        await repost_bot.on_raw_reaction_add(payload)
    clear.assert_awaited_once_with(src_channel, 777, repost_bot.settings.trigger_emoji)


# ---------------------------------------------------------------------------
# on_raw_reaction_remove
# ---------------------------------------------------------------------------


async def test_reaction_remove_trigger_routes_to_cleanup(repost_bot, session):
    payload = make_reaction_payload(channel_id=100)
    channel = MagicMock()
    repost_bot._resolve_channel = AsyncMock(return_value=channel)
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch("bot.discord_ingest.client.service.handle_reaction_removed", new_callable=AsyncMock) as handler,
    ):
        await repost_bot.on_raw_reaction_remove(payload)
    handler.assert_awaited_once()
    assert handler.await_args.kwargs["message_id"] == payload.message_id


async def test_reaction_remove_unresolvable_channel_skips(repost_bot):
    payload = make_reaction_payload(channel_id=100)
    repost_bot._resolve_channel = AsyncMock(return_value=None)
    with patch("bot.discord_ingest.client.service.handle_reaction_removed", new_callable=AsyncMock) as handler:
        await repost_bot.on_raw_reaction_remove(payload)
    handler.assert_not_awaited()


async def test_reaction_remove_other_emoji_ignored(repost_bot):
    payload = make_reaction_payload(emoji="\N{CROSS MARK}", channel_id=100)
    repost_bot._resolve_channel = AsyncMock()
    with patch("bot.discord_ingest.client.service.handle_reaction_removed", new_callable=AsyncMock) as handler:
        await repost_bot.on_raw_reaction_remove(payload)
    handler.assert_not_awaited()
    repost_bot._resolve_channel.assert_not_awaited()


async def test_reaction_remove_unwatched_channel_ignored(repost_bot):
    payload = make_reaction_payload(channel_id=999)
    repost_bot._resolve_channel = AsyncMock()
    await repost_bot.on_raw_reaction_remove(payload)
    repost_bot._resolve_channel.assert_not_awaited()


# ---------------------------------------------------------------------------
# on_message
# ---------------------------------------------------------------------------


def _make_message(*, author_id=7, channel_id=100, has_reference=True):
    message = MagicMock()
    message.author.id = author_id
    message.reference = MagicMock() if has_reference else None
    message.channel = MagicMock()
    message.channel.id = channel_id
    return message


async def test_on_message_ignores_own_messages(repost_bot):
    message = _make_message(author_id=42)
    with (
        _patch_user(42),
        patch("bot.discord_ingest.client.service.handle_reply", new_callable=AsyncMock) as handler,
    ):
        await repost_bot.on_message(message)
    handler.assert_not_awaited()


async def test_on_message_reply_in_watched_channel_routes(repost_bot, session):
    message = _make_message()
    repost_bot._http = MagicMock()
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch("bot.discord_ingest.client.service.handle_reply", new_callable=AsyncMock) as handler,
    ):
        await repost_bot.on_message(message)
    handler.assert_awaited_once()
    assert handler.await_args.kwargs["message"] is message


async def test_on_message_non_reply_ignored(repost_bot):
    message = _make_message(has_reference=False)
    with patch("bot.discord_ingest.client.service.handle_reply", new_callable=AsyncMock) as handler:
        await repost_bot.on_message(message)
    handler.assert_not_awaited()


async def test_on_message_unwatched_location_ignored(repost_bot):
    message = _make_message(channel_id=999)
    with patch("bot.discord_ingest.client.service.handle_reply", new_callable=AsyncMock) as handler:
        await repost_bot.on_message(message)
    handler.assert_not_awaited()
