"""One-shot script: remove all human members from threads whose submission is
already queued, published, or failed.

For each thread, first tries to list actual thread members via the Discord API
(requires MANAGE_THREADS, which the bot has). If that endpoint returns 403,
falls back to removing known users from the DB (submission author + curators).

Run locally (copy DB from server first):
    docker --context DigitalOcean-remote cp bluesky-repost-bot:/data/db/bot.db /tmp/bot.db
    DATABASE_URL=sqlite+aiosqlite:////tmp/bot.db \\
        op run --env-file op.env --no-masking -- \\
        uv run python scripts/prune_thread_members.py
"""

from __future__ import annotations

import asyncio
import logging
import sys

import discord
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

sys.path.insert(0, "src")

from bot.config import get_settings
from bot.models import Curator, Submission, SubmissionThread
from bot.state import SubmissionState

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

_TERMINAL_STATES = (
    SubmissionState.QUEUED.value,
    SubmissionState.PUBLISHED.value,
    SubmissionState.PUBLISH_FAILED.value,
)


async def fetch_data(database_url: str) -> tuple[list[tuple[int, int]], set[int]]:
    """Return (thread_id, author_id) pairs and the set of all curator user IDs."""
    engine = create_async_engine(database_url, future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        async with factory() as session:
            thread_rows = await session.execute(
                select(SubmissionThread.thread_id, Submission.author_id)
                .join(
                    Submission,
                    (Submission.board_id == SubmissionThread.board_id)
                    & (Submission.source_discord_message_id == SubmissionThread.source_discord_message_id),
                )
                .where(Submission.state.in_(_TERMINAL_STATES))
            )
            pairs = list(thread_rows.all())

            curator_rows = await session.scalars(
                select(Curator.discord_user_id).where(Curator.discord_user_id.isnot(None))
            )
            curator_ids = set(curator_rows.all())

        return pairs, curator_ids
    finally:
        await engine.dispose()


async def _member_ids_for_thread(
    client: discord.Client, thread_id: int, fallback: set[int]
) -> set[int] | None:
    """Return all user IDs currently in the thread, or None if the thread should be skipped (403)."""
    try:
        members = await client.http.get_thread_members(thread_id)
        return {int(m["user_id"]) for m in members}
    except discord.HTTPException as exc:
        if exc.status == 403:
            return None  # can't list members; caller will fall back to DB set
        raise


async def prune(client: discord.Client, pairs: list[tuple[int, int]], curator_ids: set[int]) -> None:
    await client.wait_until_ready()
    bot_id = client.user.id
    log.info("logged in as %s; %d thread(s) to process", client.user, len(pairs))

    removed = already_gone = errors = 0
    for thread_id, author_id in pairs:
        # Try to get the real member list from Discord; fall back to known DB users.
        live_ids = await _member_ids_for_thread(client, thread_id, fallback=set())
        if live_ids is None:
            # 403 on member listing - try removing known users anyway (bot has MANAGE_THREADS)
            candidates = ({author_id} | curator_ids) - {bot_id}
            log.debug("thread %d: member list 403, falling back to %d known users", thread_id, len(candidates))
        else:
            candidates = live_ids - {bot_id}
            log.debug("thread %d: %d live member(s) to remove", thread_id, len(candidates))

        for user_id in candidates:
            try:
                await client.http.remove_user_from_thread(thread_id, user_id)
                log.debug("thread %d: removed user %d", thread_id, user_id)
                removed += 1
            except discord.NotFound:
                already_gone += 1  # user wasn't in the thread — fine
            except discord.HTTPException as exc:
                if exc.status == 403:
                    log.debug("thread %d: forbidden removing user %d, skipping thread", thread_id, user_id)
                    break
                log.warning("thread %d user %d: %s", thread_id, user_id, exc)
                errors += 1

    log.info("done. removed %d; already absent %d; errors %d", removed, already_gone, errors)
    await client.close()


async def main() -> None:
    settings = get_settings()
    import os
    database_url = os.environ.get("DATABASE_URL", settings.database_url)
    log.info("querying %s", database_url)
    pairs, curator_ids = await fetch_data(database_url)
    log.info("%d terminal threads; %d individual curator IDs", len(pairs), len(curator_ids))

    intents = discord.Intents.none()
    client = discord.Client(intents=intents)
    asyncio.create_task(prune(client, pairs, curator_ids))
    await client.start(settings.discord_bot_token)


asyncio.run(main())
