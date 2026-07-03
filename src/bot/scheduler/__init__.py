"""Periodic tasks: storage heartbeat + queue dispatcher."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from sqlalchemy import select

from ..asset_store import has_free_space
from ..config import BoardConfig, Settings
from ..db import session_scope
from ..discord_ingest import service as ingest_service
from ..discord_ingest.discord_notifier import DiscordNotifier
from .. import errors as errors_module
from ..models import Board, Submission, SubmissionThread, YoutubePlaylistAdd
from ..state import PublishOutcome, SubmissionState
from .. import queue as queue_module

log = logging.getLogger(__name__)

_HEARTBEAT_SECONDS = 300
_THREAD_CLEANUP_INTERVAL = 300   # seconds between cleanup runs
_THREAD_CLEANUP_BATCH = 50       # threads to inspect per run

# States where the Discord thread should be closed.
_CLOSED_STATES = (
    SubmissionState.PUBLISHED.value,
    SubmissionState.QUEUED.value,
    SubmissionState.PUBLISH_FAILED.value,
)


async def _archive_stale_threads(bot, session) -> int:
    """Archive Discord threads for submissions that are closed on our side.

    Published and queued submissions should have archived threads; if they
    don't (archive call failed, historical accumulation, etc.) we close them
    here in batches to keep the guild's active-thread count in check.
    Returns the number of threads successfully archived this run.
    """
    from datetime import datetime, timezone as _tz
    now = datetime.now(_tz.utc)

    rows = (await session.scalars(
        select(SubmissionThread)
        .join(
            Submission,
            (SubmissionThread.board_id == Submission.board_id)
            & (SubmissionThread.source_discord_message_id == Submission.source_discord_message_id),
        )
        .where(
            Submission.state.in_(_CLOSED_STATES),
            SubmissionThread.archived_at.is_(None),
        )
        .limit(_THREAD_CLEANUP_BATCH)
    )).all()

    archived = 0
    for row in rows:
        try:
            channel = await bot.fetch_channel(row.thread_id)
        except Exception as exc:
            log.debug("thread cleanup: could not fetch %s: %s", row.thread_id, exc)
            continue
        if not isinstance(channel, discord.Thread):
            # Not a thread (e.g. already deleted); mark done so we don't retry.
            row.archived_at = now
            continue
        if channel.archived:
            row.archived_at = now
            continue
        try:
            await channel.edit(archived=True)
            row.archived_at = now
            archived += 1
            log.info("thread cleanup: archived thread %s", row.thread_id)
        except Exception as exc:
            log.warning("thread cleanup: failed to archive thread %s: %s", row.thread_id, exc)

    return archived


async def _record_discord_thread_count(bot) -> None:
    """Fetch the real active-thread count from Discord and write it to bot_status."""
    from ..bot_status import record_thread_count
    total = 0
    for guild in bot.guilds:
        try:
            threads = await guild.active_threads()
            total += len(threads)
        except Exception as exc:
            log.warning("thread cleanup: could not fetch active threads for guild %s: %s", guild.id, exc)
    record_thread_count(total)
    log.debug("thread cleanup: Discord reports %d active threads across %d guild(s)", total, len(bot.guilds))


async def run_thread_cleanup(bot, stop: asyncio.Event) -> None:
    """Periodically archive Discord threads for closed submissions and record the real count."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=_THREAD_CLEANUP_INTERVAL)
            break
        except asyncio.TimeoutError:
            pass
        try:
            async with session_scope() as session:
                n = await _archive_stale_threads(bot, session)
                if n:
                    log.info("thread cleanup: archived %d stale threads this run", n)
        except Exception:
            log.exception("thread cleanup run failed")
        try:
            await _record_discord_thread_count(bot)
        except Exception:
            log.exception("thread cleanup: failed to record Discord thread count")


async def _retry_failed_playlist_adds(yt_client, session) -> int:
    """Retry YoutubePlaylistAdd rows that failed and have no subsequent success.

    Updates the existing row in place so the dashboard reflects the outcome
    without accumulating duplicate records.
    """
    from sqlalchemy.orm import aliased
    SuccessPA = aliased(YoutubePlaylistAdd)
    # Find failed rows for which no successful row exists for the same (board_id, video_id).
    success_exists = (
        select(SuccessPA.id)
        .where(
            SuccessPA.board_id == YoutubePlaylistAdd.board_id,
            SuccessPA.video_id == YoutubePlaylistAdd.video_id,
            SuccessPA.success.is_(True),
        )
        .correlate(YoutubePlaylistAdd)
        .exists()
    )
    failed_rows = (await session.scalars(
        select(YoutubePlaylistAdd)
        .where(YoutubePlaylistAdd.success.is_(False), ~success_exists)
        .limit(50)
    )).all()

    loop = asyncio.get_running_loop()
    recovered = 0
    for row in failed_rows:
        try:
            item_id = await loop.run_in_executor(
                None, yt_client.add_to_playlist, row.playlist_id, row.video_id
            )
            row.success = True
            row.playlist_item_id = item_id
            row.error_message = None
            recovered += 1
            log.info("playlist retry: added video %s to %s", row.video_id, row.playlist_id)
        except Exception as exc:
            row.error_message = str(exc)
            log.warning("playlist retry: still failing for video %s: %s", row.video_id, exc)
    return recovered


async def run_playlist_retry(yt_client, stop: asyncio.Event) -> None:
    """Once per hour, retry any failed playlist adds that haven't since succeeded."""
    _RETRY_INTERVAL = 3600
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=_RETRY_INTERVAL)
            break
        except asyncio.TimeoutError:
            pass
        try:
            async with session_scope() as session:
                n = await _retry_failed_playlist_adds(yt_client, session)
                if n:
                    log.info("playlist retry: recovered %d failed add(s)", n)
        except Exception:
            log.exception("playlist retry run failed")


async def run_housekeeping(settings: Settings, stop: asyncio.Event) -> None:
    """Loop until ``stop`` is set, logging storage health periodically."""
    while not stop.is_set():
        ok = has_free_space(settings.data_dir, settings.storage_min_free_mb)
        if not ok:
            log.warning(
                "storage below %s MB floor at %s - new attachment downloads will be blocked",
                settings.storage_min_free_mb,
                settings.data_dir,
            )
        else:
            log.debug("heartbeat: storage ok")
        try:
            await asyncio.wait_for(stop.wait(), timeout=_HEARTBEAT_SECONDS)
        except asyncio.TimeoutError:
            continue


def _next_fire_time(now_mt: datetime, start_hour: int) -> datetime:
    """Return the next top-of-hour at or after ``start_hour`` in the same timezone as ``now_mt``."""
    next_hour = (now_mt + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    if next_hour.hour < start_hour:
        return next_hour.replace(hour=start_hour)
    return next_hour


def _mt_midnight(now_utc: datetime, tz: ZoneInfo) -> datetime:
    """Return the start of the current local day as a UTC-aware datetime."""
    midnight_local = now_utc.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight_local.astimezone(timezone.utc)


async def run_queue_dispatcher(bot, settings: Settings, stop: asyncio.Event) -> None:
    """Fire the queue tick once per hour from queue_start_hour onward in the configured timezone."""
    tz = ZoneInfo(settings.queue_timezone)
    log.info(
        "queue dispatcher starting (tz=%s start_hour=%d min=%d max=%d target_days=%d)",
        settings.queue_timezone,
        settings.queue_start_hour,
        settings.queue_min_daily,
        settings.queue_max_daily,
        settings.queue_target_days,
    )
    while not stop.is_set():
        now_mt = datetime.now(tz)
        next_fire = _next_fire_time(now_mt, settings.queue_start_hour)
        wait_sec = (next_fire.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds()
        log.debug("queue dispatcher: next fire at %s (%.0fs)", next_fire.isoformat(), wait_sec)
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(wait_sec, 0))
            break  # stop was set
        except asyncio.TimeoutError:
            pass
        await _fire_all_boards(bot, settings, tz)


async def _fire_all_boards(bot, settings: Settings, tz: ZoneInfo) -> None:
    now_utc = datetime.now(timezone.utc)
    fresh_cutoff = now_utc - timedelta(hours=settings.queue_fresh_window_hours)
    mt_midnight = _mt_midnight(now_utc, tz)
    for board_cfg in settings.boards:
        if not board_cfg.bluesky_handle:
            continue
        try:
            async with session_scope() as session:
                await _fire_board(session, bot, settings, board_cfg, fresh_cutoff, mt_midnight)
        except Exception:
            log.exception("queue tick failed for board %s", board_cfg.name)
            await errors_module.record_error("scheduler", f"board {board_cfg.name}")


async def _fire_board(
    session,
    bot,
    settings: Settings,
    board_cfg: BoardConfig,
    fresh_cutoff: datetime,
    mt_midnight: datetime,
) -> None:
    board = await session.scalar(
        select(Board).where(Board.discord_channel_id == board_cfg.discord_channel_id)
    )
    if board is None:
        log.warning("queue: no DB row for board %s", board_cfg.name)
        return

    today_count = await queue_module.count_posts_today(session, board.id, mt_midnight)
    queue_size = await queue_module.count_queued_for_board(session, board.id)
    cap = queue_module.daily_cap(queue_size, settings)

    if today_count >= cap:
        log.info("queue: board %s at daily cap (%d/%d, %d queued)", board_cfg.name, today_count, cap, queue_size)
        return

    _MAX_SKIP_TRIES = 25
    _MAX_FAILED_ATTEMPTS_PER_TICK = 3
    skip_ids: set[int] = set()
    failed_attempts = 0
    for _ in range(_MAX_SKIP_TRIES):
        submission = await queue_module.pick_next_for_board(
            session, board.id, fresh_cutoff, skip_ids=frozenset(skip_ids)
        )
        if submission is None:
            log.debug("queue: board %s nothing queued", board_cfg.name)
            return

        log.info(
            "queue: board %s publishing submission %s (%d/%d today, %d queued)",
            board_cfg.name, submission.id, today_count + 1, cap, queue_size,
        )

        destination = None
        thread_row = await session.scalar(
            select(SubmissionThread).where(
                SubmissionThread.board_id == submission.board_id,
                SubmissionThread.source_discord_message_id == submission.source_discord_message_id,
            )
        )
        if thread_row is not None:
            try:
                channel = await bot.fetch_channel(thread_row.thread_id)
                destination = DiscordNotifier(channel)
            except Exception as exc:
                log.warning(
                    "queue: could not resolve thread for submission %s: %s - publishing silently",
                    submission.id, exc,
                )

        outcome = await ingest_service.publish_queued_submission(session, settings, submission, destination)
        if outcome is PublishOutcome.PUBLISHED:
            return  # posted - the tick is spent

        skip_ids.add(submission.id)
        if outcome is PublishOutcome.FAILED:
            # Fall through to the next item so one permanently-failing submission
            # can't starve the board, but bound the damage: a systemic failure
            # (bad credentials, network down) shouldn't burn through the queue
            # marking everything PUBLISH_FAILED in a single tick.
            failed_attempts += 1
            if failed_attempts >= _MAX_FAILED_ATTEMPTS_PER_TICK:
                log.warning(
                    "queue: board %s giving up this tick after %d failed publish attempts",
                    board_cfg.name, failed_attempts,
                )
                return
            log.info(
                "queue: board %s submission %s failed to publish, trying next in queue (%d/%d failures this tick)",
                board_cfg.name, submission.id, failed_attempts, _MAX_FAILED_ATTEMPTS_PER_TICK,
            )
        elif outcome is PublishOutcome.DUPLICATE:
            log.info(
                "queue: board %s submission %s was a duplicate, trying next in queue",
                board_cfg.name, submission.id,
            )
        else:  # DEFERRED
            log.info(
                "queue: board %s submission %s deferred (parent not published), trying next",
                board_cfg.name, submission.id,
            )

    log.info("queue: board %s hit skip limit (%d) - deferreds/duplicates blocking queue", board_cfg.name, _MAX_SKIP_TRIES)
