"""Process entrypoint. Wires config, DB, the Discord client, and housekeeping."""

from __future__ import annotations

import asyncio
import logging

from .config import get_settings
from .db import dispose_engine, init_engine
from .discord_ingest import RepostBot
from .logging_setup import configure_logging
from .scheduler import run_housekeeping

log = logging.getLogger(__name__)


async def amain() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.logs_dir)
    init_engine(settings.database_url)

    if not settings.boards:
        log.warning("no boards configured (BOARDS_JSON is empty) - nothing will be watched")

    bot = RepostBot(settings)
    stop = asyncio.Event()
    housekeeping = asyncio.create_task(run_housekeeping(settings, stop))

    try:
        await bot.start(settings.discord_bot_token)
    finally:
        stop.set()
        housekeeping.cancel()
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
