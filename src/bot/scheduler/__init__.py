"""Periodic tasks: storage heartbeat + queue dispatcher."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from ..asset_store import has_free_space
from ..config import BoardConfig, Settings
from ..db import session_scope
from ..discord_ingest import service as ingest_service
from .. import errors as errors_module
from ..models import Board, Submission, SubmissionThread
from .. import queue as queue_module

log = logging.getLogger(__name__)

_HEARTBEAT_SECONDS = 300


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
        "queue dispatcher starting (tz=%s start_hour=%d fresh_cap=%d backlog_cap=%d)",
        settings.queue_timezone,
        settings.queue_start_hour,
        settings.queue_fresh_daily_cap,
        settings.queue_backlog_daily_cap,
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
    fresh_available = await queue_module.has_fresh_queued(session, board.id, fresh_cutoff)
    cap = queue_module.daily_cap(fresh_available, settings)

    if today_count >= cap:
        log.info("queue: board %s at daily cap (%d/%d)", board_cfg.name, today_count, cap)
        return

    _MAX_DEFERRED_TRIES = 5
    skip_ids: set[int] = set()
    for _ in range(_MAX_DEFERRED_TRIES):
        submission = await queue_module.pick_next_for_board(
            session, board.id, fresh_cutoff, skip_ids=frozenset(skip_ids)
        )
        if submission is None:
            log.debug("queue: board %s nothing queued", board_cfg.name)
            return

        log.info(
            "queue: board %s publishing submission %s (%d/%d today)",
            board_cfg.name, submission.id, today_count + 1, cap,
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
                destination = await bot.fetch_channel(thread_row.thread_id)
            except Exception as exc:
                log.warning(
                    "queue: could not resolve thread for submission %s: %s - publishing silently",
                    submission.id, exc,
                )

        attempted = await ingest_service.publish_queued_submission(session, settings, submission, destination)
        if attempted:
            return
        skip_ids.add(submission.id)
        log.info(
            "queue: board %s submission %s deferred (parent not published), trying next",
            board_cfg.name, submission.id,
        )

    log.info("queue: board %s all %d candidates deferred", board_cfg.name, _MAX_DEFERRED_TRIES)
