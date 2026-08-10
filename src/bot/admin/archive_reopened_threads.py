"""One-off: re-archive submission threads that were wrongly reopened (issue #65).

A pre-fix bot version (before 1.13.10) reopened a large batch of previously-archived
threads: its startup catch-up blanket-unarchived every pending thread and edited the
status checklist, which un-archives a Discord thread. The 1.13.10 fix stops *new*
reopens but cannot re-close threads that were already open when it deployed, and the
periodic scheduler only archives closed-state (queued/published) submissions - never
the non-terminal ones this hit. This script closes the leftovers.

Selection is idle-only, matching what Discord's own inactivity policy would have kept
archived, so a thread a curator is actively working in is left open:

  * the submission is non-terminal (anything but queued/published/publish_failed) and
    has a thread,
  * the thread is currently open (not archived), and
  * its last message is older than --idle-hours (default 24).

Targets are archived silently (no closing notice - posting into a large batch would
itself be noise, and read like the reopen churn we are undoing). Idempotent: already
-archived and still-active threads are skipped, so it is safe to re-run.

Usage (on the deploy host):

    docker exec bluesky-repost-bot python -m bot.admin.archive_reopened_threads --dry-run
    docker exec bluesky-repost-bot python -m bot.admin.archive_reopened_threads
    docker exec bluesky-repost-bot python -m bot.admin.archive_reopened_threads --idle-hours 48
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import discord
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import dispose_engine, init_engine, session_scope
from ..discord_ingest.service import _archive_thread
from ..models import Submission
from ..scheduler import _CLOSED_STATES

log = logging.getLogger(__name__)


def _is_idle(last_activity: datetime, now: datetime, idle_hours: int) -> bool:
    """Whether a thread's most recent activity is old enough to re-archive."""
    return now - last_activity >= timedelta(hours=idle_hours)


async def find_targets(session: AsyncSession) -> list[tuple[int, int]]:
    """Non-terminal submissions that still have a thread. Returns (submission_id, thread_id).

    Terminal (closed) submissions are handled by the scheduler's cleanup; we only chase
    the non-terminal threads the reopen bug touched. The idle/open check happens later,
    against live Discord state.
    """
    rows = await session.execute(
        select(Submission.id, Submission.thread_id)
        .where(
            ~Submission.state.in_(_CLOSED_STATES),
            Submission.thread_id.is_not(None),
        )
        .order_by(Submission.id)
    )
    return [(r.id, r.thread_id) for r in rows]


async def _last_activity(thread: discord.Thread) -> datetime:
    """Timestamp of the thread's most recent message, or the thread's own creation
    time when it has no messages (both are timezone-aware UTC)."""
    async for message in thread.history(limit=1):
        return message.created_at
    return thread.created_at


async def amain(dry_run: bool, idle_hours: int) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s :: %(message)s")
    settings = get_settings()
    init_engine(settings.database_url)

    async with session_scope() as session:
        targets = await find_targets(session)

    log.info("%d non-terminal thread(s) to check (idle cutoff: %dh)", len(targets), idle_hours)

    client = discord.Client(intents=discord.Intents.none())
    await client.login(settings.discord_bot_token)
    now = datetime.now(timezone.utc)
    archived = skipped_open_active = skipped_already = errors = 0
    try:
        for submission_id, thread_id in targets:
            try:
                thread = await client.fetch_channel(thread_id)
            except discord.NotFound:
                log.info("submission %s: thread %s already gone", submission_id, thread_id)
                continue
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning("submission %s: could not fetch thread %s (%s)", submission_id, thread_id, exc)
                errors += 1
                continue

            if not isinstance(thread, discord.Thread):
                continue
            if thread.archived:
                skipped_already += 1
                continue

            try:
                last = await _last_activity(thread)
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning("submission %s: could not read history of thread %s (%s)", submission_id, thread_id, exc)
                errors += 1
                continue

            age_h = (now - last).total_seconds() / 3600
            if not _is_idle(last, now, idle_hours):
                skipped_open_active += 1
                continue

            if dry_run:
                log.info("would archive thread %s (submission %s, idle %.1fh)", thread_id, submission_id, age_h)
                archived += 1
                continue

            try:
                await _archive_thread(thread, notice=None)  # silent
                archived += 1
                log.info("archived thread %s (submission %s, idle %.1fh)", thread_id, submission_id, age_h)
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning("submission %s: failed to archive thread %s (%s)", submission_id, thread_id, exc)
                errors += 1

        verb = "would archive" if dry_run else "archived"
        log.info(
            "done: %s %d, skipped %d (active) + %d (already archived), %d error(s)",
            verb, archived, skipped_open_active, skipped_already, errors,
        )
    finally:
        await client.close()
        await dispose_engine()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="list threads that would be archived without touching Discord state")
    parser.add_argument("--idle-hours", type=int, default=24,
                        help="only archive threads whose last message is older than this (default: 24)")
    args = parser.parse_args()
    asyncio.run(amain(dry_run=args.dry_run, idle_hours=args.idle_hours))


if __name__ == "__main__":
    main()
