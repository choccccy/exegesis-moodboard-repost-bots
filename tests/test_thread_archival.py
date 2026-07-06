"""Tests for thread archival helpers in the ingest service.

Covers _fire_and_forget (background task scheduling), the delayed and
immediate archive helpers, _unarchive_thread, _clear_trigger_reaction, and
_resolve_thread_by_id.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import discord

import bot.discord_ingest.service as service
from bot.discord_ingest.service import (
    _archive_thread,
    _archive_thread_after_delay_seconds,
    _clear_trigger_reaction,
    _fire_and_forget,
    _resolve_thread_by_id,
    _unarchive_thread,
)


def _thread(*, archived=False, thread_id=555) -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    thread.archived = archived
    thread.send = AsyncMock()
    thread.edit = AsyncMock()
    return thread


async def _drain():
    for _ in range(5):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# _fire_and_forget
# ---------------------------------------------------------------------------


async def test_fire_and_forget_runs_coroutine():
    ran = asyncio.Event()

    async def work():
        ran.set()

    _fire_and_forget(work())
    await _drain()
    assert ran.is_set()


async def test_fire_and_forget_exception_not_raised():
    started = asyncio.Event()

    async def bad():
        started.set()
        raise ValueError("boom")

    _fire_and_forget(bad())
    task = next(iter(service._background_tasks))  # grab before done-callback discards
    await _drain()
    assert started.is_set()
    assert task.done()
    assert isinstance(task.exception(), ValueError)  # contained in the task, not raised
    assert task not in service._background_tasks  # done callback cleaned up


# ---------------------------------------------------------------------------
# _archive_thread_after_delay_seconds
# ---------------------------------------------------------------------------


async def test_delayed_archive_sleeps_then_archives():
    thread = _thread()
    with patch("bot.discord_ingest.service.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        await _archive_thread_after_delay_seconds(thread, 900)
    mock_sleep.assert_awaited_once_with(900)
    thread.edit.assert_awaited_once_with(archived=True)


async def test_delayed_archive_zero_delay_skips_sleep():
    thread = _thread()
    with patch("bot.discord_ingest.service.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        await _archive_thread_after_delay_seconds(thread, 0)
    mock_sleep.assert_not_awaited()
    thread.edit.assert_awaited_once_with(archived=True)


async def test_delayed_archive_sends_notice_first():
    thread = _thread()
    with patch("bot.discord_ingest.service.asyncio.sleep", new_callable=AsyncMock):
        await _archive_thread_after_delay_seconds(thread, 10, notice="closing up")
    thread.send.assert_awaited_once_with("closing up")
    thread.edit.assert_awaited_once_with(archived=True)


async def test_delayed_archive_notice_failure_still_archives():
    thread = _thread()
    thread.send = AsyncMock(side_effect=RuntimeError("send failed"))
    with patch("bot.discord_ingest.service.asyncio.sleep", new_callable=AsyncMock):
        await _archive_thread_after_delay_seconds(thread, 10, notice="closing up")
    thread.edit.assert_awaited_once_with(archived=True)


async def test_delayed_archive_edit_failure_swallowed():
    thread = _thread()
    thread.edit = AsyncMock(side_effect=RuntimeError("edit failed"))
    with patch("bot.discord_ingest.service.asyncio.sleep", new_callable=AsyncMock):
        await _archive_thread_after_delay_seconds(thread, 10)  # must not raise
    thread.edit.assert_awaited_once()


# ---------------------------------------------------------------------------
# _archive_thread / _unarchive_thread
# ---------------------------------------------------------------------------


async def test_archive_thread_archives_open_thread():
    thread = _thread(archived=False)
    await _archive_thread(thread, notice="all done")
    thread.send.assert_awaited_once_with("all done")
    thread.edit.assert_awaited_once_with(archived=True)


async def test_archive_thread_already_archived_is_noop():
    thread = _thread(archived=True)
    await _archive_thread(thread, notice="all done")
    thread.send.assert_not_awaited()
    thread.edit.assert_not_awaited()


async def test_archive_thread_notice_failure_still_archives():
    thread = _thread()
    thread.send = AsyncMock(side_effect=RuntimeError("no perms"))
    await _archive_thread(thread, notice="all done")
    thread.edit.assert_awaited_once_with(archived=True)


async def test_archive_thread_edit_failure_swallowed():
    thread = _thread()
    thread.edit = AsyncMock(side_effect=RuntimeError("no perms"))
    await _archive_thread(thread)  # must not raise


async def test_unarchive_thread_reopens():
    thread = _thread(archived=True)
    await _unarchive_thread(thread)
    thread.edit.assert_awaited_once_with(archived=False)


async def test_unarchive_thread_open_thread_is_noop():
    thread = _thread(archived=False)
    await _unarchive_thread(thread)
    thread.edit.assert_not_awaited()


async def test_unarchive_thread_edit_failure_swallowed():
    thread = _thread(archived=True)
    thread.edit = AsyncMock(side_effect=RuntimeError("no perms"))
    await _unarchive_thread(thread)  # must not raise


# ---------------------------------------------------------------------------
# _clear_trigger_reaction
# ---------------------------------------------------------------------------


async def test_clear_trigger_reaction_success():
    msg = MagicMock()
    msg.clear_reaction = AsyncMock()
    channel = MagicMock()
    channel.fetch_message = AsyncMock(return_value=msg)

    await _clear_trigger_reaction(channel, 42, "\N{BUTTERFLY}")

    channel.fetch_message.assert_awaited_once_with(42)
    msg.clear_reaction.assert_awaited_once_with("\N{BUTTERFLY}")


async def test_clear_trigger_reaction_forbidden_swallowed():
    channel = MagicMock()
    channel.fetch_message = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "no perms"))
    await _clear_trigger_reaction(channel, 42, "\N{BUTTERFLY}")  # must not raise


async def test_clear_trigger_reaction_not_found_swallowed():
    msg = MagicMock()
    msg.clear_reaction = AsyncMock(side_effect=discord.NotFound(MagicMock(), "gone"))
    channel = MagicMock()
    channel.fetch_message = AsyncMock(return_value=msg)
    await _clear_trigger_reaction(channel, 42, "\N{BUTTERFLY}")  # must not raise


# ---------------------------------------------------------------------------
# _resolve_thread_by_id
# ---------------------------------------------------------------------------


async def test_resolve_thread_no_guild_returns_none():
    channel = MagicMock()
    channel.guild = None
    assert await _resolve_thread_by_id(channel, 9999) is None


async def test_resolve_thread_returns_cached_thread():
    thread = _thread(thread_id=9999)
    channel = MagicMock()
    channel.guild.get_thread = MagicMock(return_value=thread)
    channel.guild.fetch_channel = AsyncMock()

    result = await _resolve_thread_by_id(channel, 9999)

    assert result is thread
    channel.guild.fetch_channel.assert_not_awaited()


async def test_resolve_thread_fetches_when_not_cached():
    thread = _thread(thread_id=9999)
    channel = MagicMock()
    channel.guild.get_thread = MagicMock(return_value=None)
    channel.guild.fetch_channel = AsyncMock(return_value=thread)

    result = await _resolve_thread_by_id(channel, 9999)

    assert result is thread
    channel.guild.fetch_channel.assert_awaited_once_with(9999)


async def test_resolve_thread_non_thread_channel_returns_none():
    not_a_thread = MagicMock(spec=discord.TextChannel)
    channel = MagicMock()
    channel.guild.get_thread = MagicMock(return_value=None)
    channel.guild.fetch_channel = AsyncMock(return_value=not_a_thread)

    assert await _resolve_thread_by_id(channel, 9999) is None


async def test_resolve_thread_fetch_error_returns_none():
    channel = MagicMock()
    channel.guild.get_thread = MagicMock(return_value=None)
    channel.guild.fetch_channel = AsyncMock(side_effect=discord.NotFound(MagicMock(), "gone"))

    assert await _resolve_thread_by_id(channel, 9999) is None
