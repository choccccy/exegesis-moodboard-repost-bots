"""Backfill native video attachments onto already-ingested link submissions.

Before v1.7.0, video/GIF link submissions (twitter, tiktok, reddit) resolved to
only a thumbnail still. This one-shot script finds unpublished submissions
whose source is a video post, re-resolves them, and attaches the actual video
so they publish as native Bluesky video.

Candidate selection is deliberately narrow so we only re-hit APIs where it can
matter:
  - twitter: only links whose stored resolved_image_url is on one of twitter's
    video-thumbnail CDN paths (tweet_video_thumb / ext_tw_video_thumb /
    amplify_video_thumb) - photo tweets are skipped without any API call.
  - tiktok: every TikTok is a video, so all qualify.
  - reddit: needs one JSON API call to know; only is_gif posts yield a video.
Submissions that already have a video attachment (uploaded or backfilled) are
skipped, so the script is safe to re-run.

Usage (on the deploy host):

    docker exec bluesky-repost-bot python -m bot.admin.backfill_videos --dry-run
    docker exec bluesky-repost-bot python -m bot.admin.backfill_videos
    docker exec bluesky-repost-bot python -m bot.admin.backfill_videos --limit 10
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
from ..discord_ingest.service import _ingest_resolved_video
from ..models import Attachment, Submission, SubmissionLink
from ..resolve import resolve
from ..state import SubmissionState

log = logging.getLogger(__name__)

_FAMILIES = ("twitter", "tiktok", "reddit")

# Twitter serves video/GIF thumbnails from these CDN paths; a stored thumbnail
# URL on one of them proves the tweet had video media (photos use /media/).
_TWITTER_VIDEO_THUMB_MARKERS = (
    "tweet_video_thumb",      # GIFs
    "ext_tw_video_thumb",     # native videos
    "amplify_video_thumb",    # pro/ads videos
)

# Politeness delay between resolver API calls (tikwm rate-limits at ~1 req/s).
_SLEEP_BETWEEN_CALLS = 1.2


async def find_candidates(session: AsyncSession) -> list[tuple[int, int, str, str]]:
    """Return (submission_id, link_id, family, canonical_url) for backfillable submissions.

    A submission qualifies when it is not yet published, its primary link is in
    a video-capable family, it has no video attachment yet, and (for twitter)
    the stored thumbnail URL proves the tweet actually had video media.
    """
    has_video = (
        select(Attachment.id)
        .where(
            Attachment.submission_id == Submission.id,
            Attachment.is_video.is_(True),
        )
        .exists()
    )
    rows = (
        await session.execute(
            select(Submission.id, SubmissionLink.id, SubmissionLink.domain_family,
                   SubmissionLink.canonical_url, SubmissionLink.resolved_image_url)
            .join(SubmissionLink, SubmissionLink.submission_id == Submission.id)
            .where(
                SubmissionLink.order_index == 0,
                SubmissionLink.domain_family.in_(_FAMILIES),
                Submission.state != SubmissionState.PUBLISHED.value,
                ~has_video,
            )
            .order_by(Submission.id)
        )
    ).all()

    out: list[tuple[int, int, str, str]] = []
    for sub_id, link_id, family, canonical_url, resolved_image_url in rows:
        if family == "twitter":
            img = resolved_image_url or ""
            if not any(marker in img for marker in _TWITTER_VIDEO_THUMB_MARKERS):
                continue  # photo tweet (or no media): nothing to backfill
        if not canonical_url:
            continue
        out.append((sub_id, link_id, family, canonical_url))
    return out


async def amain(dry_run: bool, limit: int | None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s :: %(message)s")
    settings = get_settings()
    init_engine(settings.database_url)
    attached = no_video = failed = 0
    try:
        async with session_scope() as session:
            candidates = await find_candidates(session)
        if limit is not None:
            candidates = candidates[:limit]

        by_family: dict[str, int] = {}
        for _, _, family, _ in candidates:
            by_family[family] = by_family.get(family, 0) + 1
        log.info("found %d candidate submission(s): %s", len(candidates), by_family or "none")

        if dry_run:
            for sub_id, _, family, url in candidates:
                log.info("would re-resolve submission %s (%s): %s", sub_id, family, url)
            return

        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            for sub_id, link_id, family, url in candidates:
                try:
                    meta = await resolve(url, family, client=client,
                                         youtube_api_key=settings.youtube_api_key)
                except Exception as exc:
                    log.warning("resolve failed for submission %s (%s): %s", sub_id, url, exc)
                    failed += 1
                    continue
                if not meta.video_url:
                    log.info("submission %s (%s): no video in source, skipping", sub_id, family)
                    no_video += 1
                    continue
                try:
                    async with session_scope() as session:
                        sub = await session.get(Submission, sub_id)
                        link = await session.get(SubmissionLink, link_id)
                        if sub is None or link is None:
                            continue
                        await _ingest_resolved_video(session, sub, link, meta, settings, client)
                        # _ingest_resolved_video degrades silently (download error,
                        # oversize); verify a row actually appeared before counting it.
                        got_video = (await session.scalar(
                            select(Attachment.id).where(
                                Attachment.submission_id == sub_id,
                                Attachment.is_video.is_(True),
                            )
                        )) is not None
                    if got_video:
                        attached += 1
                        log.info("submission %s: video attached", sub_id)
                    else:
                        failed += 1
                        log.info("submission %s: video download/size check failed, thumbnail kept", sub_id)
                except Exception as exc:
                    log.warning("video ingest failed for submission %s: %s", sub_id, exc)
                    failed += 1
                await asyncio.sleep(_SLEEP_BETWEEN_CALLS)

        log.info("done: %d attached, %d had no video, %d failed", attached, no_video, failed)
    finally:
        await dispose_engine()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="list candidates without calling APIs or writing anything")
    parser.add_argument("--limit", type=int, default=None,
                        help="process at most N candidates")
    args = parser.parse_args()
    asyncio.run(amain(dry_run=args.dry_run, limit=args.limit))


if __name__ == "__main__":
    main()
