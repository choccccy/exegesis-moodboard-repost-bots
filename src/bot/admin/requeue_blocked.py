"""Requeue submissions parked in PUBLISH_BLOCKED once the underlying issue is fixed.

A publish that failed for a permanent, our-side reason (bad app password, invalid
content, malformed URL) is moved to ``PUBLISH_BLOCKED`` and deliberately dropped
from the queue so it does not retry on its own. After a curator fixes the cause -
e.g. rotates the Bluesky app password, or corrects a link - this flips the blocked
submissions back to ``PUBLISH_FAILED`` so the normal hourly dispatcher picks them
up again at the next slot (PUBLISH_FAILED is queue-eligible; PUBLISH_BLOCKED is not).

Requeue all blocked submissions, or scope to one board or specific ids.

Usage (on the deploy host):

    docker exec bluesky-repost-bot python -m bot.admin.requeue_blocked --dry-run
    docker exec bluesky-repost-bot python -m bot.admin.requeue_blocked
    docker exec bluesky-repost-bot python -m bot.admin.requeue_blocked --board robots
    docker exec bluesky-repost-bot python -m bot.admin.requeue_blocked --ids 1234 1235
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from sqlalchemy import select

from ..config import get_settings
from ..db import dispose_engine, init_engine, session_scope
from ..models import Board, Submission
from ..state import SubmissionState

log = logging.getLogger(__name__)


async def amain(dry_run: bool, board: str | None, ids: list[int] | None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s :: %(message)s")
    settings = get_settings()
    init_engine(settings.database_url)

    try:
        async with session_scope() as session:
            board_id = None
            if board is not None:
                board_id = await session.scalar(select(Board.id).where(Board.name == board))
                if board_id is None:
                    log.error("no board named %r", board)
                    return

            stmt = select(Submission).where(
                Submission.state == SubmissionState.PUBLISH_BLOCKED.value
            )
            if board_id is not None:
                stmt = stmt.where(Submission.board_id == board_id)
            if ids:
                stmt = stmt.where(Submission.id.in_(ids))
            blocked = (await session.scalars(stmt)).all()

            log.info("%d blocked submission(s) to requeue", len(blocked))
            for sub in blocked:
                if dry_run:
                    log.info("would requeue submission %s (board %s)", sub.id, sub.board_id)
                    continue
                sub.state = SubmissionState.PUBLISH_FAILED.value
                log.info("requeued submission %s (board %s)", sub.id, sub.board_id)
    finally:
        await dispose_engine()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="list what would be requeued without changing anything")
    parser.add_argument("--board", default=None,
                        help="limit to a single board by name (default: all boards)")
    parser.add_argument("--ids", nargs="*", type=int, default=None,
                        help="limit to specific submission ids")
    args = parser.parse_args()
    asyncio.run(amain(dry_run=args.dry_run, board=args.board, ids=args.ids))


if __name__ == "__main__":
    main()
