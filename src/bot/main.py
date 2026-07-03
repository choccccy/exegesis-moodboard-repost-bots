"""Process entrypoint. Wires config, DB, the Discord client, and housekeeping."""

from __future__ import annotations

import asyncio
import logging

from .bot_status import init as init_bot_status
from .config import get_settings
from .db import dispose_engine, init_engine
from .discord_ingest import RepostBot
from .logging_setup import configure_logging
from .scheduler import run_housekeeping, run_queue_dispatcher, run_thread_cleanup, run_playlist_retry
from .youtube import YouTubeClient

log = logging.getLogger(__name__)


def _on_task_done(task: asyncio.Task, name: str) -> None:
    """Log an error if a background task exits unexpectedly (not via cancellation)."""
    if not task.cancelled() and (exc := task.exception()):
        log.error("background task %r died unexpectedly", name, exc_info=exc)


def _watched_task(coro, name: str) -> asyncio.Task:
    task = asyncio.create_task(coro, name=name)
    task.add_done_callback(lambda t: _on_task_done(t, name))
    return task


async def amain() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.logs_dir)
    init_engine(settings.database_url)
    init_bot_status(settings.data_dir)

    if not settings.boards:
        log.warning("no boards configured (BOARDS_JSON is empty) - nothing will be watched")

    yt_client: YouTubeClient | None = None
    if all([settings.youtube_client_id, settings.youtube_client_secret, settings.youtube_refresh_token]):
        yt_client = YouTubeClient(
            settings.youtube_client_id,  # type: ignore[arg-type]
            settings.youtube_client_secret,  # type: ignore[arg-type]
            settings.youtube_refresh_token,  # type: ignore[arg-type]
        )
        log.info("YouTube playlist client initialized")
    else:
        log.info("YouTube playlist client not configured (missing OAuth2 credentials)")

    bot = RepostBot(settings, yt_client=yt_client)
    stop = asyncio.Event()
    housekeeping = _watched_task(run_housekeeping(settings, stop), "housekeeping")
    queue_dispatcher = _watched_task(run_queue_dispatcher(bot, settings, stop), "queue_dispatcher")
    thread_cleanup = _watched_task(run_thread_cleanup(bot, stop), "thread_cleanup")
    playlist_retry = _watched_task(run_playlist_retry(yt_client, stop), "playlist_retry") if yt_client else None

    try:
        await bot.start(settings.discord_bot_token)
    finally:
        stop.set()
        housekeeping.cancel()
        queue_dispatcher.cancel()
        thread_cleanup.cancel()
        if playlist_retry:
            playlist_retry.cancel()
        if not bot.is_closed():
            await bot.close()
        await dispose_engine()


def run() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
