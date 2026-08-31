"""One-time (idempotent) publisher for the feed-generator records.

Publishes one ``app.bsky.feed.generator`` record per feed in ``bot.feedgen.feeds.FEEDS``
into the owner account's repo (choccy.gay). This is what makes each feed show up in-app
as "by @owner". ``put_record`` overwrites by rkey, so re-running is safe and is how you
rename/retitle a feed later.

The record's ``did`` points at the *service* DID (``did:web:<hostname>``); the running
feed service holds no credentials - only this script does, and only to publish.

Env:
    FEEDGEN_OWNER_HANDLE        e.g. choccy.gay
    FEEDGEN_OWNER_APP_PASSWORD  app password for that account (this script only)
    FEEDGEN_SERVICE_HOSTNAME    default feed.exegesis.space

Usage (owner creds injected via `op run`):

    op run --env-file op.env --no-masking -- \\
      docker --context DigitalOcean-remote exec -e FEEDGEN_OWNER_HANDLE -e FEEDGEN_OWNER_APP_PASSWORD \\
      bluesky-repost-bot python -m bot.admin.publish_feedgen --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import logging
import os

from atproto import AsyncClient, models

from ..feedgen.feeds import FEEDS

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


async def amain(dry_run: bool) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s :: %(message)s")

    handle = os.environ["FEEDGEN_OWNER_HANDLE"]
    password = os.environ["FEEDGEN_OWNER_APP_PASSWORD"]
    hostname = os.environ.get("FEEDGEN_SERVICE_HOSTNAME", "feed.exegesis.space")
    service_did = f"did:web:{hostname}"

    client = AsyncClient()
    await client.login(handle, password)
    owner_did = client.me.did
    log.info("logged in as %s (%s); service DID %s", handle, owner_did, service_did)

    for feed in FEEDS.values():
        feed_uri = f"at://{owner_did}/app.bsky.feed.generator/{feed.rkey}"
        if dry_run:
            log.info("would put_record %s -> %r", feed_uri, feed.display_name)
            continue
        record = models.AppBskyFeedGenerator.Record(
            did=service_did,
            display_name=feed.display_name,
            description=feed.description,
            created_at=_now_iso(),
        )
        await client.com.atproto.repo.put_record(
            models.ComAtprotoRepoPutRecord.Data(
                repo=owner_did,
                collection=models.ids.AppBskyFeedGenerator,
                rkey=feed.rkey,
                record=record,
            )
        )
        log.info("published %s -> %r", feed_uri, feed.display_name)

    log.info("---")
    log.info("FEEDGEN_OWNER_DID=%s", owner_did)
    for feed in FEEDS.values():
        log.info("feed: at://%s/app.bsky.feed.generator/%s", owner_did, feed.rkey)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would be published without writing records")
    args = parser.parse_args()
    asyncio.run(amain(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
