"""Process entrypoint. Wires config, DB, the Discord client, and housekeeping."""

from __future__ import annotations

import asyncio
import logging

from .bot_status import init as init_bot_status
from .config import get_settings
from .db import dispose_engine, init_engine
from .discord_ingest import RepostBot
from .logging_setup import configure_logging
from .scheduler import run_housekeeping, run_queue_dispatcher
from .youtube import YouTubeClient

log = logging.getLogger(__name__)


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
    housekeeping = asyncio.create_task(run_housekeeping(settings, stop))
    queue_dispatcher = asyncio.create_task(run_queue_dispatcher(bot, settings, stop))

    try:
        await bot.start(settings.discord_bot_token)
    finally:
        stop.set()
        housekeeping.cancel()
        queue_dispatcher.cancel()
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
