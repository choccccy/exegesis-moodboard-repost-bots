"""Backfill the pinned DID (source_at_uri) onto already-ingested Bluesky links.

Before v1.10.3, Bluesky source links stored only the handle-based canonical_url
and resolved the handle to a DID live at publish time. If the source account
renamed or deactivated its handle in the meantime, the repost failed permanently
(the handle no longer resolves), even though the post still exists under its
stable DID. Ingest now pins the DID at capture; this one-shot script does the
same for links captured earlier so they're protected before their handle can die.

Candidate selection is narrow and safe:
  - only bluesky links whose source_at_uri is still NULL (so it is idempotent and
    re-runnable - already-pinned links are skipped);
  - only submissions not yet published (a published repost is already done).
Resolution is authoritative only (com.atproto.identity.resolveHandle). A link
whose handle no longer resolves stays NULL and is reported as unresolved - there
is no safe automatic recovery for a dead handle (it may have been recycled to a
different account), so those need a human to repoint them at the live handle.

Usage (on the deploy host):

    docker exec bluesky-repost-bot python -m bot.admin.backfill_bsky_did --dry-run
    docker exec bluesky-repost-bot python -m bot.admin.backfill_bsky_did
    docker exec bluesky-repost-bot python -m bot.admin.backfill_bsky_did --limit 10
"""

from __future__ import annotations

import argparse
import asyncio
import logging

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import dispose_engine, init_engine, session_scope
from ..models import Submission, SubmissionLink
from ..resolve import resolve_bluesky_at_uri
from ..state import SubmissionState

log = logging.getLogger(__name__)

# resolveHandle on the public appview is cheap and generously rate-limited
# (3000 req / 5 min); a small delay keeps us well-mannered without dragging.
_SLEEP_BETWEEN_CALLS = 0.1


async def find_candidates(session: AsyncSession) -> list[tuple[int, int, str]]:
    """Return (submission_id, link_id, canonical_url) for un-pinned bluesky links.

    Every bluesky link with no source_at_uri on a not-yet-published submission
    qualifies (ingest pins all bluesky links, not just the primary, so the
    backfill matches).
    """
    rows = (
        await session.execute(
            select(Submission.id, SubmissionLink.id, SubmissionLink.canonical_url)
            .join(SubmissionLink, SubmissionLink.submission_id == Submission.id)
            .where(
                SubmissionLink.domain_family == "bluesky",
                SubmissionLink.source_at_uri.is_(None),
                SubmissionLink.canonical_url.is_not(None),
                Submission.state != SubmissionState.PUBLISHED.value,
            )
            .order_by(Submission.id)
        )
    ).all()
    return [(sub_id, link_id, url) for sub_id, link_id, url in rows]


async def amain(dry_run: bool, limit: int | None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s :: %(message)s")
    settings = get_settings()
    init_engine(settings.database_url)
    pinned = unresolved = failed = 0
    try:
        async with session_scope() as session:
            candidates = await find_candidates(session)
        if limit is not None:
            candidates = candidates[:limit]
        log.info("found %d un-pinned bluesky link(s)", len(candidates))

        if dry_run:
            for sub_id, _, url in candidates:
                log.info("would resolve+pin DID for submission %s: %s", sub_id, url)
            return

        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            for sub_id, link_id, url in candidates:
                try:
                    at_uri = await resolve_bluesky_at_uri(url, client)
                except Exception as exc:  # resolve_bluesky_at_uri already swallows the expected ones
                    log.warning("resolve errored for submission %s (%s): %s", sub_id, url, exc)
                    failed += 1
                    continue
                if at_uri is None:
                    log.info(
                        "submission %s: handle in %s no longer resolves - left NULL, needs manual repoint",
                        sub_id, url,
                    )
                    unresolved += 1
                    continue
                async with session_scope() as session:
                    link = await session.get(SubmissionLink, link_id)
                    if link is None:
                        continue
                    link.source_at_uri = at_uri
                pinned += 1
                log.info("submission %s: pinned %s", sub_id, at_uri)
                await asyncio.sleep(_SLEEP_BETWEEN_CALLS)

        log.info("done: %d pinned, %d unresolved (dead handle), %d errored", pinned, unresolved, failed)
    finally:
        await dispose_engine()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="list candidates without calling the appview or writing anything")
    parser.add_argument("--limit", type=int, default=None,
                        help="process at most N candidates")
    args = parser.parse_args()
    asyncio.run(amain(dry_run=args.dry_run, limit=args.limit))


if __name__ == "__main__":
    main()
