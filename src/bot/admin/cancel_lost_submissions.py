"""One-off: cancel pre-fix bluesky submissions whose source post no longer exists
anywhere (account deleted, or renamed-and-recreated so the original post is gone).

These were identified by authoritative triage: the post rkey is present in no
findable repo, so there is nothing to repost. This deletes the submission (DB rows
+ downloaded assets, same cascade as a ❌ cancel) and closes its Discord thread
with a closing notice.

Ordering is deliberate: the thread is closed FIRST, and the DB row is deleted only
if the close succeeded or the thread was already gone. A transient Discord failure
therefore skips that submission entirely (retry later) rather than deleting its row
and orphaning an open thread. Idempotent: already-deleted submissions are skipped.

Usage (on the deploy host):

    docker exec bluesky-repost-bot python -m bot.admin.cancel_lost_submissions --dry-run
    docker exec bluesky-repost-bot python -m bot.admin.cancel_lost_submissions
"""

from __future__ import annotations

import argparse
import asyncio
import logging

import discord

from ..asset_store import remove_submission_dir
from ..config import get_settings
from ..db import dispose_engine, init_engine, session_scope
from ..discord_ingest import replies
from ..discord_ingest.service import _archive_thread, _delete_submission_cascade
from ..models import Submission
from ..state import SubmissionState

log = logging.getLogger(__name__)

# Submissions whose source post is gone (triage-confirmed unrecoverable).
_LOST_IDS = [
    516, 1026, 1048, 1131, 1385, 1274, 1275, 1323, 1454, 1488,
    1491, 1558, 3261, 1475, 1577, 2989, 3307, 3312, 3529,
]

_NOTICE = replies.closing_notice("source no longer exists")


async def amain(dry_run: bool) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s :: %(message)s")
    settings = get_settings()
    init_engine(settings.database_url)

    # Snapshot the per-submission facts we need before deleting anything.
    async with session_scope() as session:
        targets = []  # (sub_id, board_id, thread_id)
        for sid in _LOST_IDS:
            sub = await session.get(Submission, sid)
            if sub is None:
                log.info("submission %s already gone - skipping", sid)
                continue
            if sub.state == SubmissionState.PUBLISHED.value:
                log.warning("submission %s is PUBLISHED - refusing to cancel", sid)
                continue
            targets.append((sid, sub.board_id, sub.thread_id))

    log.info("%d submission(s) to cancel", len(targets))
    if dry_run:
        for sid, _, thread_id in targets:
            log.info("would cancel submission %s (thread %s)", sid, thread_id or "none")
        await dispose_engine()
        return

    client = discord.Client(intents=discord.Intents.none())
    await client.login(settings.discord_bot_token)
    cancelled = skipped = 0
    try:
        for sid, board_id, thread_id in targets:
            # 1. Close the Discord thread first (if any).
            if thread_id is not None:
                try:
                    thread = await client.fetch_channel(thread_id)
                    await _archive_thread(thread, notice=_NOTICE)
                except discord.NotFound:
                    log.info("submission %s: thread %s already gone", sid, thread_id)
                except (discord.Forbidden, discord.HTTPException) as exc:
                    log.warning("submission %s: thread close failed (%s) - skipping delete, retry later",
                                sid, exc)
                    skipped += 1
                    continue
            # 2. Only now delete DB rows + assets.
            async with session_scope() as session:
                remove_submission_dir(settings.attachments_dir, board_id, sid)
                await _delete_submission_cascade(session, sid)
            cancelled += 1
            log.info("cancelled submission %s", sid)
        log.info("done: %d cancelled, %d skipped", cancelled, skipped)
    finally:
        await client.close()
        await dispose_engine()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="list what would be cancelled without deleting or touching Discord")
    args = parser.parse_args()
    asyncio.run(amain(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
