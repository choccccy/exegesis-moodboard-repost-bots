"""Conformance tests for the DiscordSurface / DiscordNotifier outbound adapters
(surface-agnostic core, issue #50 Phase B). Each Surface method must translate to the
right Discord call and map Discord's exceptions to the port's documented return values.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot.components import Button, PreviewImage
from bot.discord_ingest.discord_notifier import DiscordNotifier, DiscordSurface
from bot.notifier import NullSurface

pytestmark = pytest.mark.asyncio


def _http_exc(kind):
    """Build a discord HTTP-family exception without a real response object."""
    resp = MagicMock()
    resp.status = 404 if kind is discord.NotFound else 403
    return kind(resp, "boom")


def _thread():
    t = MagicMock(spec=discord.Thread)
    t.send = AsyncMock(return_value=MagicMock(id=1))
    return t


def _partial(edit_side_effect=None):
    """A channel whose get_partial_message(...).edit is an AsyncMock."""
    channel = MagicMock(spec=discord.TextChannel)
    partial = MagicMock()
    partial.edit = AsyncMock(side_effect=edit_side_effect)
    channel.get_partial_message = MagicMock(return_value=partial)
    return channel, partial


# --- send -------------------------------------------------------------------

async def test_send_plain_content_no_view_no_file():
    channel = _thread()
    surface = DiscordSurface(channel)
    await surface.send("hello")
    channel.send.assert_awaited_once()
    assert channel.send.await_args.args[0] == "hello"
    assert "view" not in channel.send.await_args.kwargs
    assert "file" not in channel.send.await_args.kwargs


async def test_send_with_components_passes_rendered_view():
    channel = _thread()
    surface = DiscordSurface(channel)
    await surface.send("q", components=[Button(label="Queue", action_id="confirm:1")])
    view = channel.send.await_args.kwargs["view"]
    assert isinstance(view, discord.ui.View)
    assert view.children[0].custom_id == "confirm:1"


async def test_send_with_preview_attaches_rendered_file():
    channel = _thread()
    surface = DiscordSurface(channel)
    sentinel = MagicMock()
    with patch("bot.discord_ingest.discord_notifier.render_preview", return_value=sentinel) as rp:
        await surface.send("look", preview=PreviewImage(local_path="/x.png", filename="x.png"))
    rp.assert_called_once()
    assert channel.send.await_args.kwargs["file"] is sentinel


# --- edit -------------------------------------------------------------------

async def test_edit_returns_true_on_success():
    channel, partial = _partial()
    assert await DiscordSurface(channel).edit(5, content="new") is True
    partial.edit.assert_awaited_once()


async def test_edit_returns_false_on_discord_error():
    channel, _ = _partial(edit_side_effect=_http_exc(discord.Forbidden))
    assert await DiscordSurface(channel).edit(5, content="new") is False


# --- disable_components -----------------------------------------------------

async def test_disable_components_tombstones_and_returns_true():
    channel, partial = _partial()
    assert await DiscordSurface(channel).disable_components(5, "Queued ✅") is True
    view = partial.edit.await_args.kwargs["view"]
    assert view.children[0].disabled is True and view.children[0].label == "Queued ✅"


async def test_disable_components_returns_false_on_error():
    channel, _ = _partial(edit_side_effect=_http_exc(discord.NotFound))
    assert await DiscordSurface(channel).disable_components(5, "x") is False


# --- edit_or_none -----------------------------------------------------------

async def test_edit_or_none_true_on_success():
    channel, _ = _partial()
    assert await DiscordSurface(channel).edit_or_none(5, "c") is True


async def test_edit_or_none_false_when_message_gone():
    channel, _ = _partial(edit_side_effect=_http_exc(discord.NotFound))
    assert await DiscordSurface(channel).edit_or_none(5, "c") is False


async def test_edit_or_none_true_on_transient_error():
    # A transient error must NOT respawn the message, so it reports True (edited-ish).
    channel, _ = _partial(edit_side_effect=_http_exc(discord.HTTPException))
    assert await DiscordSurface(channel).edit_or_none(5, "c") is True


# --- message_exists ---------------------------------------------------------

async def test_message_exists_true_when_found():
    channel = MagicMock(spec=discord.TextChannel)
    channel.fetch_message = AsyncMock(return_value=MagicMock())
    assert await DiscordSurface(channel).message_exists(5) is True


async def test_message_exists_false_when_not_found():
    channel = MagicMock(spec=discord.TextChannel)
    channel.fetch_message = AsyncMock(side_effect=_http_exc(discord.NotFound))
    assert await DiscordSurface(channel).message_exists(5) is False


async def test_message_exists_true_when_unverifiable():
    channel = MagicMock(spec=discord.TextChannel)
    channel.fetch_message = AsyncMock(side_effect=_http_exc(discord.Forbidden))
    assert await DiscordSurface(channel).message_exists(5) is True


# --- archive / unarchive lifecycle ------------------------------------------

async def test_archive_calls_service_helper_for_threads():
    thread = _thread()
    with patch("bot.discord_ingest.service._archive_thread", new_callable=AsyncMock) as arch:
        await DiscordSurface(thread).archive(notice="done")
    arch.assert_awaited_once()
    assert arch.await_args.kwargs["notice"] == "done"


async def test_archive_noop_for_non_thread():
    channel = MagicMock(spec=discord.TextChannel)
    with patch("bot.discord_ingest.service._archive_thread", new_callable=AsyncMock) as arch:
        await DiscordSurface(channel).archive()
    arch.assert_not_awaited()


async def test_archive_after_delay_calls_helper_for_threads():
    thread = _thread()
    with patch("bot.discord_ingest.service._archive_thread_after_delay") as arch:
        DiscordSurface(thread).archive_after_delay(notice="soon")
    arch.assert_called_once()


async def test_archive_after_delay_with_explicit_delay_uses_seconds_helper():
    # An explicit delay (e.g. remaining close time on an already-queued thread) routes
    # to the seconds-parameterized helper instead of the fixed default.
    thread = _thread()
    with (
        patch("bot.discord_ingest.service._archive_thread_after_delay_seconds") as secs,
        patch("bot.discord_ingest.service._fire_and_forget") as fire,
    ):
        secs.return_value = MagicMock()  # avoid building a real coroutine
        DiscordSurface(thread).archive_after_delay(delay=42.0)
    secs.assert_called_once()
    assert secs.call_args.args[1] == 42.0
    fire.assert_called_once()


async def test_archive_after_delay_noop_for_non_thread():
    channel = MagicMock(spec=discord.TextChannel)
    with patch("bot.discord_ingest.service._archive_thread_after_delay") as arch:
        DiscordSurface(channel).archive_after_delay()
    arch.assert_not_called()


async def test_unarchive_calls_helper_for_threads():
    thread = _thread()
    with patch("bot.discord_ingest.service._unarchive_thread", new_callable=AsyncMock) as un:
        await DiscordSurface(thread).unarchive()
    un.assert_awaited_once()


async def test_unarchive_noop_for_non_thread():
    channel = MagicMock(spec=discord.TextChannel)
    with patch("bot.discord_ingest.service._unarchive_thread", new_callable=AsyncMock) as un:
        await DiscordSurface(channel).unarchive()
    un.assert_not_awaited()


# --- clear_trigger (acts on the *source* channel via the client) ------------

async def test_clear_trigger_skipped_without_client():
    with patch("bot.discord_ingest.service._clear_trigger_reaction", new_callable=AsyncMock) as clr:
        await DiscordSurface(_thread(), client=None).clear_trigger(10, 20, "🦋")
    clr.assert_not_awaited()


async def test_clear_trigger_uses_cached_channel():
    client = MagicMock()
    src = MagicMock()
    client.get_channel = MagicMock(return_value=src)
    with patch("bot.discord_ingest.service._clear_trigger_reaction", new_callable=AsyncMock) as clr:
        await DiscordSurface(_thread(), client=client).clear_trigger(10, 20, "🦋")
    clr.assert_awaited_once_with(src, 20, "🦋")


async def test_clear_trigger_fetches_channel_when_uncached():
    client = MagicMock()
    src = MagicMock()
    client.get_channel = MagicMock(return_value=None)
    client.fetch_channel = AsyncMock(return_value=src)
    with patch("bot.discord_ingest.service._clear_trigger_reaction", new_callable=AsyncMock) as clr:
        await DiscordSurface(_thread(), client=client).clear_trigger(10, 20, "🦋")
    clr.assert_awaited_once_with(src, 20, "🦋")


async def test_clear_trigger_gives_up_when_channel_unreachable():
    client = MagicMock()
    client.get_channel = MagicMock(return_value=None)
    client.fetch_channel = AsyncMock(side_effect=_http_exc(discord.Forbidden))
    with patch("bot.discord_ingest.service._clear_trigger_reaction", new_callable=AsyncMock) as clr:
        await DiscordSurface(_thread(), client=client).clear_trigger(10, 20, "🦋")
    clr.assert_not_awaited()


# --- DiscordNotifier (legacy narrow adapter) --------------------------------

async def test_discord_notifier_send_delegates_to_channel():
    channel = _thread()
    await DiscordNotifier(channel).send("hi", view=None)
    channel.send.assert_awaited_once()


async def test_discord_notifier_archive_schedules_for_thread():
    thread = _thread()
    with patch("bot.discord_ingest.service._archive_thread_after_delay") as arch:
        await DiscordNotifier(thread).archive("bye")
    arch.assert_called_once()


async def test_discord_notifier_archive_noop_for_non_thread():
    channel = MagicMock(spec=discord.TextChannel)
    with patch("bot.discord_ingest.service._archive_thread_after_delay") as arch:
        await DiscordNotifier(channel).archive("bye")
    arch.assert_not_called()


# --- NullSurface (scheduler fallback when there's no channel to post into) ---

async def test_null_surface_swallows_every_operation():
    s = NullSurface()
    assert (await s.send("x", components=[Button(label="b", action_id="a")])).id == 0
    assert await s.edit(1, content="c") is False
    assert await s.disable_components(1, "l") is False
    assert await s.edit_or_none(1, "c") is False
    assert await s.message_exists(1) is True  # can't disprove existence -> assume yes
    # lifecycle + clear_trigger are silent no-ops
    await s.archive()
    s.archive_after_delay()
    await s.unarchive()
    await s.clear_trigger(1, 2, "🦋")
