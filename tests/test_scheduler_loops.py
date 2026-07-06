"""Tests for scheduler background loops and their helper coroutines.

Covers _archive_stale_threads, _record_discord_thread_count,
run_thread_cleanup, _retry_failed_playlist_adds, run_playlist_retry,
run_housekeeping, run_queue_dispatcher's loop body, and _fire_all_boards.
The _fire_board publish path itself is covered in test_integration_scheduler.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot.config import Settings
from bot.models import SubmissionThread, YoutubePlaylistAdd
from bot.scheduler import (
    _archive_stale_threads,
    _fire_all_boards,
    _record_discord_thread_count,
    _retry_failed_playlist_adds,
    run_housekeeping,
    run_playlist_retry,
    run_queue_dispatcher,
    run_thread_cleanup,
)
from bot.state import SubmissionState

from conftest import bound_session_scope, make_one_shot_wait_for, make_submission

QUEUED = SubmissionState.QUEUED.value
PUBLISHED = SubmissionState.PUBLISHED.value


def make_stop_during_wait_for(stop: asyncio.Event):
    """Fake asyncio.wait_for simulating stop being set while waiting.

    Returns normally (no TimeoutError), so the loop takes its break path
    instead of running the body.
    """

    async def _fake_wait_for(awaitable, timeout):
        awaitable.close()
        stop.set()
        return None

    return _fake_wait_for


# ---------------------------------------------------------------------------
# _archive_stale_threads
# ---------------------------------------------------------------------------


async def _seed_thread(session, board, *, msg_id, state):
    """Add a Submission in ``state`` plus its SubmissionThread (archived_at=None)."""
    sub = make_submission(board, state=state, source_discord_message_id=msg_id)
    session.add(sub)
    row = SubmissionThread(
        board_id=board.id,
        source_discord_message_id=msg_id,
        thread_id=7000 + msg_id,
    )
    session.add(row)
    await session.flush()
    return row


def _thread_channel(*, archived=False):
    channel = MagicMock(spec=discord.Thread)
    channel.archived = archived
    channel.edit = AsyncMock()
    return channel


async def test_archive_fetch_failure_skips_row(session, board):
    row = await _seed_thread(session, board, msg_id=1, state=PUBLISHED)
    bot = MagicMock()
    bot.fetch_channel = AsyncMock(side_effect=Exception("unknown channel"))

    archived = await _archive_stale_threads(bot, session)

    assert archived == 0
    assert row.archived_at is None, "unfetchable thread must be retried next run"


async def test_archive_non_thread_channel_stamped_without_edit(session, board):
    row = await _seed_thread(session, board, msg_id=2, state=PUBLISHED)
    not_a_thread = MagicMock(spec=discord.TextChannel)
    bot = MagicMock()
    bot.fetch_channel = AsyncMock(return_value=not_a_thread)

    archived = await _archive_stale_threads(bot, session)

    assert archived == 0
    assert row.archived_at is not None, "non-thread channels are marked done, not retried"


async def test_archive_already_archived_channel_stamped(session, board):
    row = await _seed_thread(session, board, msg_id=3, state=QUEUED)
    channel = _thread_channel(archived=True)
    bot = MagicMock()
    bot.fetch_channel = AsyncMock(return_value=channel)

    archived = await _archive_stale_threads(bot, session)

    assert archived == 0
    assert row.archived_at is not None
    channel.edit.assert_not_awaited()


async def test_archive_edit_success_stamps_and_counts(session, board):
    row = await _seed_thread(session, board, msg_id=4, state=PUBLISHED)
    channel = _thread_channel(archived=False)
    bot = MagicMock()
    bot.fetch_channel = AsyncMock(return_value=channel)

    archived = await _archive_stale_threads(bot, session)

    assert archived == 1
    assert row.archived_at is not None
    channel.edit.assert_awaited_once_with(archived=True)


async def test_archive_edit_failure_warns_and_leaves_row(session, board, caplog):
    row = await _seed_thread(session, board, msg_id=5, state=PUBLISHED)
    channel = _thread_channel(archived=False)
    channel.edit = AsyncMock(side_effect=Exception("rate limited"))
    bot = MagicMock()
    bot.fetch_channel = AsyncMock(return_value=channel)

    with caplog.at_level(logging.WARNING, logger="bot.scheduler"):
        archived = await _archive_stale_threads(bot, session)

    assert archived == 0
    assert row.archived_at is None, "failed archives must be retried next run"
    assert "failed to archive thread" in caplog.text


async def test_archive_ignores_open_state_submissions(session, board):
    row = await _seed_thread(
        session, board, msg_id=6, state=SubmissionState.INTENT_SUBMITTED.value
    )
    bot = MagicMock()
    bot.fetch_channel = AsyncMock()

    archived = await _archive_stale_threads(bot, session)

    assert archived == 0
    assert row.archived_at is None
    bot.fetch_channel.assert_not_awaited()  # open submissions must keep their threads


# ---------------------------------------------------------------------------
# _record_discord_thread_count
# ---------------------------------------------------------------------------


async def test_record_thread_count_guild_failure_warns_but_records(caplog):
    broken = MagicMock(spec=discord.Guild)
    broken.active_threads = AsyncMock(side_effect=Exception("HTTP 503"))
    healthy = MagicMock(spec=discord.Guild)
    healthy.active_threads = AsyncMock(return_value=[MagicMock(), MagicMock()])
    bot = MagicMock()
    bot.guilds = [broken, healthy]

    with patch("bot.bot_status.record_thread_count") as mock_record:
        with caplog.at_level(logging.WARNING, logger="bot.scheduler"):
            await _record_discord_thread_count(bot)

    assert "could not fetch active threads" in caplog.text
    mock_record.assert_called_once_with(2)


# ---------------------------------------------------------------------------
# run_thread_cleanup
# ---------------------------------------------------------------------------


async def test_thread_cleanup_one_iteration_logs_archived_count(session, caplog):
    stop = asyncio.Event()
    bot = MagicMock()

    with (
        patch("bot.scheduler.asyncio.wait_for", make_one_shot_wait_for(stop)),
        patch("bot.scheduler.session_scope", bound_session_scope(session)),
        patch(
            "bot.scheduler._archive_stale_threads",
            new_callable=AsyncMock, return_value=2,
        ) as mock_archive,
        patch(
            "bot.scheduler._record_discord_thread_count", new_callable=AsyncMock
        ) as mock_record,
    ):
        with caplog.at_level(logging.INFO, logger="bot.scheduler"):
            await run_thread_cleanup(bot, stop)

    mock_archive.assert_awaited_once()
    mock_record.assert_awaited_once()
    assert "archived 2 stale threads" in caplog.text


async def test_thread_cleanup_survives_failures_in_both_steps(session, caplog):
    stop = asyncio.Event()
    bot = MagicMock()

    with (
        patch("bot.scheduler.asyncio.wait_for", make_one_shot_wait_for(stop)),
        patch("bot.scheduler.session_scope", bound_session_scope(session)),
        patch(
            "bot.scheduler._archive_stale_threads",
            new_callable=AsyncMock, side_effect=Exception("db exploded"),
        ),
        patch(
            "bot.scheduler._record_discord_thread_count",
            new_callable=AsyncMock, side_effect=Exception("discord exploded"),
        ),
    ):
        with caplog.at_level(logging.ERROR, logger="bot.scheduler"):
            await run_thread_cleanup(bot, stop)

    assert "thread cleanup run failed" in caplog.text
    assert "failed to record Discord thread count" in caplog.text


async def test_thread_cleanup_pre_set_stop_exits_without_body():
    stop = asyncio.Event()
    stop.set()

    with patch(
        "bot.scheduler._archive_stale_threads", new_callable=AsyncMock
    ) as mock_archive:
        await run_thread_cleanup(MagicMock(), stop)

    mock_archive.assert_not_awaited()


async def test_thread_cleanup_stop_during_wait_breaks_before_body():
    stop = asyncio.Event()

    with (
        patch("bot.scheduler.asyncio.wait_for", make_stop_during_wait_for(stop)),
        patch(
            "bot.scheduler._archive_stale_threads", new_callable=AsyncMock
        ) as mock_archive,
    ):
        await run_thread_cleanup(MagicMock(), stop)

    mock_archive.assert_not_awaited()


# ---------------------------------------------------------------------------
# run_playlist_retry
# ---------------------------------------------------------------------------


async def test_playlist_retry_one_iteration_logs_recovered(session, caplog):
    stop = asyncio.Event()

    with (
        patch("bot.scheduler.asyncio.wait_for", make_one_shot_wait_for(stop)),
        patch("bot.scheduler.session_scope", bound_session_scope(session)),
        patch(
            "bot.scheduler._retry_failed_playlist_adds",
            new_callable=AsyncMock, return_value=3,
        ) as mock_retry,
    ):
        with caplog.at_level(logging.INFO, logger="bot.scheduler"):
            await run_playlist_retry(MagicMock(), stop)

    mock_retry.assert_awaited_once()
    assert "recovered 3 failed add(s)" in caplog.text


async def test_playlist_retry_body_failure_logged_not_raised(session, caplog):
    stop = asyncio.Event()

    with (
        patch("bot.scheduler.asyncio.wait_for", make_one_shot_wait_for(stop)),
        patch("bot.scheduler.session_scope", bound_session_scope(session)),
        patch(
            "bot.scheduler._retry_failed_playlist_adds",
            new_callable=AsyncMock, side_effect=Exception("quota exceeded"),
        ),
    ):
        with caplog.at_level(logging.ERROR, logger="bot.scheduler"):
            await run_playlist_retry(MagicMock(), stop)

    assert "playlist retry run failed" in caplog.text


async def test_playlist_retry_pre_set_stop_exits_without_body():
    stop = asyncio.Event()
    stop.set()

    with patch(
        "bot.scheduler._retry_failed_playlist_adds", new_callable=AsyncMock
    ) as mock_retry:
        await run_playlist_retry(MagicMock(), stop)

    mock_retry.assert_not_awaited()


async def test_playlist_retry_stop_during_wait_breaks_before_body():
    stop = asyncio.Event()

    with (
        patch("bot.scheduler.asyncio.wait_for", make_stop_during_wait_for(stop)),
        patch(
            "bot.scheduler._retry_failed_playlist_adds", new_callable=AsyncMock
        ) as mock_retry,
    ):
        await run_playlist_retry(MagicMock(), stop)

    mock_retry.assert_not_awaited()


# ---------------------------------------------------------------------------
# _retry_failed_playlist_adds
# ---------------------------------------------------------------------------


def _playlist_add(board, *, video_id, success, msg_id=1, **kw):
    defaults = dict(
        board_id=board.id,
        source_discord_message_id=msg_id,
        video_id=video_id,
        playlist_id="PLtest",
        discord_requester_id=999,
        success=success,
    )
    defaults.update(kw)
    return YoutubePlaylistAdd(**defaults)


async def test_playlist_retry_recovers_failed_row_in_place(session, board):
    row = _playlist_add(board, video_id="vid1", success=False, error_message="boom")
    session.add(row)
    await session.flush()

    yt = MagicMock()
    yt.add_to_playlist.return_value = "item-abc"

    recovered = await _retry_failed_playlist_adds(yt, session)

    assert recovered == 1
    yt.add_to_playlist.assert_called_once_with("PLtest", "vid1")
    assert row.success is True
    assert row.playlist_item_id == "item-abc"
    assert row.error_message is None


async def test_playlist_retry_still_failing_updates_error(session, board):
    row = _playlist_add(board, video_id="vid2", success=False, error_message="old error")
    session.add(row)
    await session.flush()

    yt = MagicMock()
    yt.add_to_playlist.side_effect = RuntimeError("quota exceeded")

    recovered = await _retry_failed_playlist_adds(yt, session)

    assert recovered == 0
    assert row.success is False
    assert row.error_message == "quota exceeded"


async def test_playlist_retry_skips_row_with_later_success(session, board):
    failed = _playlist_add(board, video_id="vid3", success=False, error_message="boom")
    succeeded = _playlist_add(
        board, video_id="vid3", success=True, msg_id=2, playlist_item_id="item-ok"
    )
    session.add_all([failed, succeeded])
    await session.flush()

    yt = MagicMock()

    recovered = await _retry_failed_playlist_adds(yt, session)

    assert recovered == 0
    yt.add_to_playlist.assert_not_called()
    assert failed.success is False, "row with a later success must be left alone"


# ---------------------------------------------------------------------------
# run_housekeeping
# ---------------------------------------------------------------------------


def _housekeeping_settings():
    settings = MagicMock(spec=Settings)
    settings.data_dir = "/tmp/data"
    settings.storage_min_free_mb = 100
    return settings


async def test_housekeeping_storage_ok_logs_heartbeat(caplog):
    # wait_for sits at the END of the loop body, so a one-shot wait_for lets
    # the storage check run exactly once before the loop exits.
    stop = asyncio.Event()

    with (
        patch("bot.scheduler.has_free_space", return_value=True) as mock_free,
        patch("bot.scheduler.asyncio.wait_for", make_one_shot_wait_for(stop)),
    ):
        with caplog.at_level(logging.DEBUG, logger="bot.scheduler"):
            await run_housekeeping(_housekeeping_settings(), stop)

    mock_free.assert_called_once_with("/tmp/data", 100)
    assert "heartbeat: storage ok" in caplog.text


async def test_housekeeping_low_storage_warns(caplog):
    stop = asyncio.Event()

    with (
        patch("bot.scheduler.has_free_space", return_value=False),
        patch("bot.scheduler.asyncio.wait_for", make_one_shot_wait_for(stop)),
    ):
        with caplog.at_level(logging.WARNING, logger="bot.scheduler"):
            await run_housekeeping(_housekeeping_settings(), stop)

    assert "storage below" in caplog.text


# ---------------------------------------------------------------------------
# run_queue_dispatcher loop body
# ---------------------------------------------------------------------------


def _dispatcher_settings():
    settings = MagicMock(spec=Settings)
    settings.queue_timezone = "America/Denver"
    settings.queue_start_hour = 12
    settings.queue_min_daily = 1
    settings.queue_max_daily = 6
    settings.queue_target_days = 90
    settings.queue_fresh_window_hours = 72
    settings.boards = []
    return settings


async def test_queue_dispatcher_tick_fires_all_boards():
    stop = asyncio.Event()

    with (
        patch("bot.scheduler.asyncio.wait_for", make_one_shot_wait_for(stop)),
        patch("bot.scheduler._fire_all_boards", new_callable=AsyncMock) as mock_fire,
    ):
        await run_queue_dispatcher(MagicMock(), _dispatcher_settings(), stop)

    mock_fire.assert_awaited_once()


async def test_queue_dispatcher_stop_during_wait_breaks_before_tick():
    stop = asyncio.Event()

    with (
        patch("bot.scheduler.asyncio.wait_for", make_stop_during_wait_for(stop)),
        patch("bot.scheduler._fire_all_boards", new_callable=AsyncMock) as mock_fire,
    ):
        await run_queue_dispatcher(MagicMock(), _dispatcher_settings(), stop)

    mock_fire.assert_not_awaited()


# ---------------------------------------------------------------------------
# _fire_board skip limit (not covered by the integration tests)
# ---------------------------------------------------------------------------


async def test_fire_board_hits_skip_limit_on_endless_duplicates(session, board, caplog):
    from datetime import datetime, timedelta, timezone

    from bot.scheduler import _fire_board
    from bot.state import PublishOutcome

    # 25 queued submissions, every publish attempt reports DUPLICATE: the tick
    # must give up at the skip limit instead of looping forever.
    for i in range(25):
        session.add(make_submission(
            board, state=QUEUED, source_discord_message_id=400 + i,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=i),
        ))
    await session.flush()

    now = datetime(2026, 6, 23, 18, 0, 0, tzinfo=timezone.utc)
    fake_settings = MagicMock()
    fake_settings.queue_target_days = 90
    fake_settings.queue_min_daily = 1
    fake_settings.queue_max_daily = 6
    cfg = _board_cfg(board.name, "robots.exegesis.space")
    cfg.discord_channel_id = board.discord_channel_id

    with patch(
        "bot.scheduler.ingest_service.publish_queued_submission",
        new_callable=AsyncMock, return_value=PublishOutcome.DUPLICATE,
    ) as mock_pub:
        with caplog.at_level(logging.INFO, logger="bot.scheduler"):
            await _fire_board(
                session, MagicMock(), fake_settings, cfg,
                now - timedelta(hours=72), now.replace(hour=0),
            )

    assert mock_pub.await_count == 25
    assert "hit skip limit" in caplog.text


# ---------------------------------------------------------------------------
# _fire_all_boards
# ---------------------------------------------------------------------------


def _board_cfg(name, handle):
    cfg = MagicMock()
    cfg.name = name
    cfg.bluesky_handle = handle
    return cfg


async def test_fire_all_boards_skips_boards_without_handle(session):
    from zoneinfo import ZoneInfo

    with_handle = _board_cfg("robots", "robots.exegesis.space")
    without_handle = _board_cfg("drafts", None)
    settings = MagicMock(spec=Settings)
    settings.queue_fresh_window_hours = 72
    settings.boards = [without_handle, with_handle]

    with (
        patch("bot.scheduler.session_scope", bound_session_scope(session)),
        patch("bot.scheduler._fire_board", new_callable=AsyncMock) as mock_fire,
    ):
        await _fire_all_boards(MagicMock(), settings, ZoneInfo("UTC"))

    mock_fire.assert_awaited_once()
    assert mock_fire.await_args.args[3] is with_handle


async def test_fire_all_boards_records_error_on_board_failure(session, caplog):
    from zoneinfo import ZoneInfo

    settings = MagicMock(spec=Settings)
    settings.queue_fresh_window_hours = 72
    settings.boards = [_board_cfg("robots", "robots.exegesis.space")]

    with (
        patch("bot.scheduler.session_scope", bound_session_scope(session)),
        patch(
            "bot.scheduler._fire_board",
            new_callable=AsyncMock, side_effect=Exception("publish exploded"),
        ),
        patch(
            "bot.scheduler.errors_module.record_error", new_callable=AsyncMock
        ) as mock_record,
    ):
        with caplog.at_level(logging.ERROR, logger="bot.scheduler"):
            await _fire_all_boards(MagicMock(), settings, ZoneInfo("UTC"))

    assert "queue tick failed for board robots" in caplog.text
    mock_record.assert_awaited_once_with("scheduler", "board robots")
