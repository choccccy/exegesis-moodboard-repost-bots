"""Bulk-recover ``source_at_uri`` for native reposts that lack it.

The feed generator emits the *original post* URI for reposts, read from the
``order_index == 0`` bluesky link's ``source_at_uri``. Rows captured before DID pinning
have that column null, so those reposts are excluded from the feed. This script recovers
them in bulk: for each such repost it re-resolves the link's ``canonical_url``
(handle -> DID) and verifies the post still exists, then pins ``source_at_uri`` - exactly
the fallback path ``publish._resolve_bluesky_post`` uses at publish time.

Rows whose handle no longer resolves or whose post was deleted are left for the manual,
authoritative ``bot.admin.repoint_dead_handles``.

Usage (on the deploy host):

    docker exec bluesky-repost-bot python -m bot.admin.backfill_repost_source_uris --dry-run
    docker exec bluesky-repost-bot python -m bot.admin.backfill_repost_source_uris
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from atproto import AsyncClient
from sqlalchemy import or_, select

from ..config import get_settings
from ..db import dispose_engine, init_engine, session_scope
from ..models import PublishAttempt, Submission, SubmissionLink
from ..publish import _resolve_bluesky_post

log = logging.getLogger(__name__)

_REPOST_MARKER = "app.bsky.feed.repost"


async def _login_any_board(client: AsyncClient) -> None:
    """Log in with the first configured board that has a handle + app password.

    Only public appview reads (resolve_handle / get_posts) are needed, but the client
    convenience methods want a session.
    """
    settings = get_settings()
    for board in settings.boards:
        password = settings.bsky_password_for(board.name)
        if board.bluesky_handle and password:
            await client.login(board.bluesky_handle, password)
            log.info("logged in as %s (board %s)", board.bluesky_handle, board.name)
            return
    raise RuntimeError("no board has both a bluesky_handle and an app password configured")


async def amain(dry_run: bool) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s :: %(message)s")
    settings = get_settings()
    init_engine(settings.database_url)

    recovered = skipped = 0
    try:
        # Collect candidate links (read-only, no network under the lock).
        async with session_scope() as session:
            candidates = (await session.execute(
                select(SubmissionLink.id, SubmissionLink.canonical_url)
                .join(Submission, Submission.id == SubmissionLink.submission_id)
                .join(PublishAttempt, PublishAttempt.submission_id == Submission.id)
                .where(
                    PublishAttempt.success.is_(True),
                    PublishAttempt.at_uri.like(f"%{_REPOST_MARKER}%"),
                    SubmissionLink.order_index == 0,
                    SubmissionLink.domain_family == "bluesky",
                    or_(
                        SubmissionLink.source_at_uri.is_(None),
                        SubmissionLink.source_at_uri == "",
                    ),
                )
                .distinct()
            )).all()

        log.info("%d reposts missing source_at_uri", len(candidates))

        client = AsyncClient()
        await _login_any_board(client)

        for link_id, canonical_url in candidates:
            try:
                at_uri, _cid = await _resolve_bluesky_post(client, canonical_url)
            except Exception as exc:
                log.warning("skip link %s (%s): %s", link_id, canonical_url, exc)
                skipped += 1
                continue
            if dry_run:
                log.info("would pin link %s -> %s", link_id, at_uri)
                recovered += 1
                continue
            async with session_scope() as session:
                link = await session.get(SubmissionLink, link_id)
                if link is not None and not link.source_at_uri:
                    link.source_at_uri = at_uri
                    recovered += 1
                    log.info("pinned link %s -> %s", link_id, at_uri)

        verb = "would recover" if dry_run else "recovered"
        log.info("done: %s %d, skipped %d (dead handle / deleted post)", verb, recovered, skipped)
    finally:
        await dispose_engine()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="resolve and report without writing source_at_uri")
    args = parser.parse_args()
    asyncio.run(amain(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
