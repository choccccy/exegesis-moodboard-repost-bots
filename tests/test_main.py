"""Tests for the process entrypoint wiring in bot/main.py.

amain() is orchestration: everything external is patched, and the assertions
are about wiring - which tasks get created, that teardown always runs, and
that the watchdog callback surfaces silent task deaths (the failure mode that
hid the queue-dispatcher AttributeError crash in production).
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.main import _on_task_done, _watched_task, amain, run


# ---------------------------------------------------------------------------
# watchdog helpers
# ---------------------------------------------------------------------------


async def test_watched_task_logs_unexpected_death(caplog):
    async def boom():
        raise RuntimeError("task exploded")

    with caplog.at_level(logging.ERROR, logger="bot.main"):
        task = _watched_task(boom(), "doomed")
        with pytest.raises(RuntimeError):
            await task
        await asyncio.sleep(0)  # let the done-callback run

    assert "died unexpectedly" in caplog.text
    assert "doomed" in caplog.text


async def test_watched_task_silent_on_cancel(caplog):
    async def forever():
        await asyncio.sleep(3600)

    with caplog.at_level(logging.ERROR, logger="bot.main"):
        task = _watched_task(forever(), "cancelled-task")
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)

    assert "died unexpectedly" not in caplog.text


async def test_watched_task_silent_on_clean_exit(caplog):
    async def fine():
        return 42

    with caplog.at_level(logging.ERROR, logger="bot.main"):
        task = _watched_task(fine(), "clean")
        assert await task == 42
        await asyncio.sleep(0)

    assert caplog.text == ""


def test_on_task_done_handles_completed_task_without_exception():
    task = MagicMock()
    task.cancelled.return_value = False
    task.exception.return_value = None
    _on_task_done(task, "ok")  # must not raise or log


# ---------------------------------------------------------------------------
# amain wiring
# ---------------------------------------------------------------------------


def _main_settings(*, boards=True, youtube=False):
    s = MagicMock()
    s.log_level = "INFO"
    s.logs_dir = "/tmp/logs"
    s.database_url = "sqlite+aiosqlite:///:memory:"
    s.data_dir = "/tmp/data"
    s.boards = [MagicMock()] if boards else []
    s.discord_bot_token = "x" * 60
    s.youtube_client_id = "cid" if youtube else None
    s.youtube_client_secret = "sec" if youtube else None
    s.youtube_refresh_token = "tok" if youtube else None
    return s


def _amain_patches(settings, bot):
    return [
        patch("bot.main.get_settings", return_value=settings),
        patch("bot.main.configure_logging"),
        patch("bot.main.init_engine"),
        patch("bot.main.init_bot_status"),
        patch("bot.main.RepostBot", return_value=bot),
        patch("bot.main.YouTubeClient"),
        patch("bot.main.run_housekeeping", new=AsyncMock()),
        patch("bot.main.run_queue_dispatcher", new=AsyncMock()),
        patch("bot.main.run_thread_cleanup", new=AsyncMock()),
        patch("bot.main.run_playlist_retry", new=AsyncMock()),
        patch("bot.main.dispose_engine", new=AsyncMock()),
    ]


def _fake_bot(*, start_raises=None):
    bot = MagicMock()
    bot.start = AsyncMock(side_effect=start_raises)
    bot.close = AsyncMock()
    bot.is_closed = MagicMock(return_value=False)
    return bot


async def test_amain_starts_bot_and_tears_down():
    settings = _main_settings()
    bot = _fake_bot()
    patches = _amain_patches(settings, bot)
    with patches[0], patches[1], patches[2] as mock_engine, patches[3], patches[4], \
         patches[5], patches[6], patches[7], patches[8], patches[9], patches[10] as mock_dispose:
        await amain()

    bot.start.assert_awaited_once_with(settings.discord_bot_token)
    mock_engine.assert_called_once_with(settings.database_url)
    bot.close.assert_awaited_once()
    mock_dispose.assert_awaited_once()


async def test_amain_teardown_runs_even_when_start_raises():
    settings = _main_settings()
    bot = _fake_bot(start_raises=RuntimeError("gateway down"))
    patches = _amain_patches(settings, bot)
    with patches[0], patches[1], patches[2], patches[3], patches[4], \
         patches[5], patches[6], patches[7], patches[8], patches[9], patches[10] as mock_dispose:
        with pytest.raises(RuntimeError, match="gateway down"):
            await amain()

    bot.close.assert_awaited_once()
    mock_dispose.assert_awaited_once()


async def test_amain_no_boards_warns(caplog):
    settings = _main_settings(boards=False)
    bot = _fake_bot()
    patches = _amain_patches(settings, bot)
    with patches[0], patches[1], patches[2], patches[3], patches[4], \
         patches[5], patches[6], patches[7], patches[8], patches[9], patches[10], \
         caplog.at_level(logging.WARNING, logger="bot.main"):
        await amain()

    assert "no boards configured" in caplog.text


async def test_amain_youtube_client_and_playlist_retry_task():
    settings = _main_settings(youtube=True)
    bot = _fake_bot()
    patches = _amain_patches(settings, bot)
    with patches[0], patches[1], patches[2], patches[3], patches[4], \
         patches[5] as mock_yt, patches[6], patches[7], patches[8], \
         patches[9] as mock_retry, patches[10]:
        await amain()

    mock_yt.assert_called_once_with("cid", "sec", "tok")
    mock_retry.assert_called_once()  # playlist retry task created only with a client


async def test_amain_skips_bot_close_when_already_closed():
    settings = _main_settings()
    bot = _fake_bot()
    bot.is_closed = MagicMock(return_value=True)
    patches = _amain_patches(settings, bot)
    with patches[0], patches[1], patches[2], patches[3], patches[4], \
         patches[5], patches[6], patches[7], patches[8], patches[9], patches[10]:
        await amain()

    bot.close.assert_not_awaited()


def test_run_swallows_keyboard_interrupt():
    # amain patched to a plain MagicMock so no orphan coroutine is created.
    with patch("bot.main.asyncio.run", side_effect=KeyboardInterrupt), \
         patch("bot.main.amain", new=MagicMock()):
        run()  # must not raise
