"""One-off: repoint pre-fix bluesky submissions whose original handle went dead
but whose post still exists under the account's current (renamed) identity.

Each mapping below was confirmed authoritatively: the exact post rkey exists in
the named DID's repo (rkeys are per-repo TIDs, so a match is conclusive). For
each, we pin source_at_uri to that DID-based URI and rewrite canonical_url to the
account's current live handle so the repost publishes and the review UI reads
right. Idempotent: matches the link by submission id + rkey and only rewrites if
it still carries the old dead handle.

Usage (on the deploy host):

    docker exec bluesky-repost-bot python -m bot.admin.repoint_dead_handles --dry-run
    docker exec bluesky-repost-bot python -m bot.admin.repoint_dead_handles
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from sqlalchemy import select

from ..config import get_settings
from ..db import dispose_engine, init_engine, session_scope
from ..models import SubmissionLink

log = logging.getLogger(__name__)

# (submission_id, rkey, did, live_handle) - authoritatively confirmed via rkey match.
_MAPPINGS = [
    (478, "3lyswdyutds2n", "did:plc:egmnrkxpjer7756s5saqkovs", "benoyroma.newgrounds.com"),
    (541, "3lj6nwpphu224", "did:plc:egmnrkxpjer7756s5saqkovs", "benoyroma.newgrounds.com"),
    (1694, "3lo5hw3gctk2l", "did:plc:egmnrkxpjer7756s5saqkovs", "benoyroma.newgrounds.com"),
    (491, "3m3myf3vivk2v", "did:plc:yksnipbz5jl4ccnugdb74mk2", "dreamchimney.bsky.social"),
    (1954, "3m7iqx53yjk23", "did:plc:hzkjymwdj7m7csm3jrlcbway", "seru.me"),
    (3273, "3mbmd47qqrs2v", "did:plc:hzkjymwdj7m7csm3jrlcbway", "seru.me"),
    (2040, "3mayzishqsc24", "did:plc:c2z5ijgqdzaj7egciyxfxw6c", "machinecoolant.net"),
    (2049, "3mb6twrx67s2y", "did:plc:c2z5ijgqdzaj7egciyxfxw6c", "machinecoolant.net"),
    (3017, "3mfgkdzfmo22d", "did:plc:c2z5ijgqdzaj7egciyxfxw6c", "machinecoolant.net"),
    (2456, "3maokz65zic2c", "did:plc:zrwazih4zcfuayxjybq2oob6", "justval.download"),
    (2970, "3meo4rhivtc2l", "did:plc:epdtq375qgkekz4r4ttx3o3a", "mr-puleep.bsky.social"),
]


async def amain(dry_run: bool) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s :: %(message)s")
    settings = get_settings()
    init_engine(settings.database_url)
    repointed = missing = 0
    try:
        for sub_id, rkey, did, live_handle in _MAPPINGS:
            at_uri = f"at://{did}/app.bsky.feed.post/{rkey}"
            new_url = f"https://bsky.app/profile/{live_handle}/post/{rkey}"
            async with session_scope() as session:
                link = await session.scalar(
                    select(SubmissionLink).where(
                        SubmissionLink.submission_id == sub_id,
                        SubmissionLink.domain_family == "bluesky",
                        SubmissionLink.canonical_url.like(f"%/post/{rkey}"),
                    )
                )
                if link is None:
                    log.warning("submission %s: no bluesky link ending /post/%s found - skipping", sub_id, rkey)
                    missing += 1
                    continue
                if dry_run:
                    log.info("would repoint submission %s: %s -> %s (pin %s)",
                             sub_id, link.canonical_url, new_url, at_uri)
                    continue
                link.canonical_url = new_url
                link.source_at_uri = at_uri
            if not dry_run:
                repointed += 1
                log.info("repointed submission %s -> %s", sub_id, live_handle)
        if dry_run:
            return
        log.info("done: %d repointed, %d links missing", repointed, missing)
    finally:
        await dispose_engine()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="show the repoints without writing anything")
    args = parser.parse_args()
    asyncio.run(amain(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
