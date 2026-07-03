"""Tests for the backfill_videos admin script's candidate selection.

The selection must be narrow: only unpublished submissions whose source can
actually yield a video, and never ones that already have a video attachment.
For twitter, the stored resolved_image_url is the tell - video/GIF tweets have
thumbnails on twitter's video-thumb CDN paths; photo tweets use /media/.
"""

from __future__ import annotations

from bot.admin.backfill_videos import find_candidates
from bot.models import Attachment, SubmissionLink
from bot.state import AltTextStatus, SubmissionState

from conftest import make_submission

QUEUED = SubmissionState.QUEUED.value
PUBLISHED = SubmissionState.PUBLISHED.value


async def _add_sub_with_link(session, board, *, msg_id, family, state=QUEUED,
                             image_url=None, url="https://example.com/x"):
    sub = make_submission(board, state=state, source_discord_message_id=msg_id)
    session.add(sub)
    await session.flush()
    link = SubmissionLink(
        submission_id=sub.id,
        order_index=0,
        raw_url=url,
        canonical_url=url,
        domain_family=family,
        resolved_image_url=image_url,
    )
    session.add(link)
    await session.flush()
    return sub, link


async def test_twitter_gif_thumb_is_candidate(session, board):
    sub, _ = await _add_sub_with_link(
        session, board, msg_id=1, family="twitter",
        image_url="https://pbs.twimg.com/tweet_video_thumb/abc.jpg",
    )
    ids = [c[0] for c in await find_candidates(session)]
    assert sub.id in ids


async def test_twitter_video_thumb_is_candidate(session, board):
    sub, _ = await _add_sub_with_link(
        session, board, msg_id=2, family="twitter",
        image_url="https://pbs.twimg.com/ext_tw_video_thumb/9/pu/img/still.jpg",
    )
    ids = [c[0] for c in await find_candidates(session)]
    assert sub.id in ids


async def test_twitter_photo_is_not_candidate(session, board):
    """Photo tweets are excluded without any API call - that's the point."""
    sub, _ = await _add_sub_with_link(
        session, board, msg_id=3, family="twitter",
        image_url="https://pbs.twimg.com/media/photo.jpg",
    )
    ids = [c[0] for c in await find_candidates(session)]
    assert sub.id not in ids


async def test_tiktok_always_candidate(session, board):
    sub, _ = await _add_sub_with_link(
        session, board, msg_id=4, family="tiktok",
        image_url="https://cdn.tiktok.com/cover.jpg",
    )
    ids = [c[0] for c in await find_candidates(session)]
    assert sub.id in ids


async def test_reddit_is_candidate(session, board):
    sub, _ = await _add_sub_with_link(
        session, board, msg_id=5, family="reddit",
        image_url="https://preview.redd.it/still.jpg",
    )
    ids = [c[0] for c in await find_candidates(session)]
    assert sub.id in ids


async def test_published_submission_excluded(session, board):
    sub, _ = await _add_sub_with_link(
        session, board, msg_id=6, family="twitter", state=PUBLISHED,
        image_url="https://pbs.twimg.com/tweet_video_thumb/abc.jpg",
    )
    ids = [c[0] for c in await find_candidates(session)]
    assert sub.id not in ids


async def test_existing_video_attachment_excluded(session, board):
    """Re-running the script must not touch submissions that already have video."""
    sub, _ = await _add_sub_with_link(
        session, board, msg_id=7, family="twitter",
        image_url="https://pbs.twimg.com/tweet_video_thumb/abc.jpg",
    )
    session.add(Attachment(
        submission_id=sub.id, discord_attachment_id=0, filename="linkvid_1.mp4",
        discord_url="https://video.twimg.com/clip.mp4", is_image=False, is_video=True,
        alt_text_status=AltTextStatus.NEEDED.value,
    ))
    await session.flush()

    ids = [c[0] for c in await find_candidates(session)]
    assert sub.id not in ids


async def test_non_video_family_excluded(session, board):
    sub, _ = await _add_sub_with_link(
        session, board, msg_id=8, family="artstation",
        image_url="https://cdn.artstation.com/art.jpg",
    )
    ids = [c[0] for c in await find_candidates(session)]
    assert sub.id not in ids
