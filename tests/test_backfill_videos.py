"""Tests for the backfill_videos admin script.

Candidate selection must be narrow: only unpublished submissions whose source
can actually yield a video, and never ones that already have a video
attachment. For twitter, the stored resolved_image_url is the tell - video/GIF
tweets have thumbnails on twitter's video-thumb CDN paths; photo tweets use
/media/. Also covers the amain() driver (dry-run, attach, no-video, resolve
failure) and main() argv parsing.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select

from bot.admin.backfill_videos import amain, find_candidates, main
from bot.models import Attachment, Board, SubmissionLink
from bot.resolve import ResolvedMetadata
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


# ---------------------------------------------------------------------------
# amain() driver and main() argv parsing
# ---------------------------------------------------------------------------

async def _seed_candidate(db) -> int:
    """Insert a video-thumb twitter candidate via bot.db's global engine."""
    async with db.session_scope() as session:
        board = Board(name="robots", discord_guild_id=1, discord_channel_id=100)
        session.add(board)
        await session.flush()
        sub = make_submission(board, state=QUEUED, source_discord_message_id=1)
        session.add(sub)
        await session.flush()
        session.add(SubmissionLink(
            submission_id=sub.id, order_index=0,
            raw_url="https://x.com/u/status/1", canonical_url="https://x.com/u/status/1",
            domain_family="twitter",
            resolved_image_url="https://pbs.twimg.com/ext_tw_video_thumb/9/img/still.jpg",
        ))
        return sub.id


async def _count_video_attachments(db) -> int:
    async with db.session_scope() as session:
        rows = await session.scalars(
            select(Attachment.id).where(Attachment.is_video.is_(True))
        )
        return len(list(rows))


def _amain_patches():
    """Patch out settings/engine lifecycle so amain runs on the global_engine DB."""
    settings = MagicMock()
    settings.youtube_api_key = None
    return (
        patch("bot.admin.backfill_videos.get_settings", return_value=settings),
        patch("bot.admin.backfill_videos.init_engine"),
        patch("bot.admin.backfill_videos.dispose_engine", new=AsyncMock()),
        patch("bot.admin.backfill_videos._SLEEP_BETWEEN_CALLS", 0),
    )


async def test_amain_dry_run_writes_nothing(global_engine, caplog):
    sub_id = await _seed_candidate(global_engine)
    p_settings, p_init, p_dispose, p_sleep = _amain_patches()
    with p_settings, p_init, p_dispose, p_sleep, caplog.at_level(logging.INFO):
        await amain(dry_run=True, limit=None)

    assert f"would re-resolve submission {sub_id}" in caplog.text
    assert await _count_video_attachments(global_engine) == 0


async def test_amain_attaches_video(global_engine, caplog):
    await _seed_candidate(global_engine)

    async def fake_ingest(session, sub, link, meta, settings, client):
        session.add(Attachment(
            submission_id=sub.id, discord_attachment_id=0, filename="linkvid_1.mp4",
            discord_url=meta.video_url, is_image=False, is_video=True,
            alt_text_status=AltTextStatus.NEEDED.value,
        ))
        await session.flush()

    meta = ResolvedMetadata(video_url="https://video.twimg.com/clip.mp4")
    p_settings, p_init, p_dispose, p_sleep = _amain_patches()
    with p_settings, p_init, p_dispose, p_sleep, \
            patch("bot.admin.backfill_videos.resolve", new=AsyncMock(return_value=meta)), \
            patch("bot.admin.backfill_videos._ingest_resolved_video",
                  new=AsyncMock(side_effect=fake_ingest)), \
            caplog.at_level(logging.INFO):
        await amain(dry_run=False, limit=1)

    assert "done: 1 attached, 0 had no video, 0 failed" in caplog.text
    assert await _count_video_attachments(global_engine) == 1


async def test_amain_no_video_in_source(global_engine, caplog):
    await _seed_candidate(global_engine)

    meta = ResolvedMetadata(image_url="https://pbs.twimg.com/still.jpg")
    p_settings, p_init, p_dispose, p_sleep = _amain_patches()
    with p_settings, p_init, p_dispose, p_sleep, \
            patch("bot.admin.backfill_videos.resolve", new=AsyncMock(return_value=meta)), \
            caplog.at_level(logging.INFO):
        await amain(dry_run=False, limit=None)

    assert "done: 0 attached, 1 had no video, 0 failed" in caplog.text
    assert await _count_video_attachments(global_engine) == 0


async def test_amain_resolve_failure_counts_failed(global_engine, caplog):
    await _seed_candidate(global_engine)

    p_settings, p_init, p_dispose, p_sleep = _amain_patches()
    with p_settings, p_init, p_dispose, p_sleep, \
            patch("bot.admin.backfill_videos.resolve",
                  new=AsyncMock(side_effect=RuntimeError("api down"))), \
            caplog.at_level(logging.INFO):
        await amain(dry_run=False, limit=None)

    assert "done: 0 attached, 0 had no video, 1 failed" in caplog.text


async def test_amain_silent_ingest_degrade_counts_failed(global_engine, caplog):
    """_ingest_resolved_video can degrade silently; no attachment row = failed."""
    await _seed_candidate(global_engine)

    meta = ResolvedMetadata(video_url="https://video.twimg.com/clip.mp4")
    p_settings, p_init, p_dispose, p_sleep = _amain_patches()
    with p_settings, p_init, p_dispose, p_sleep, \
            patch("bot.admin.backfill_videos.resolve", new=AsyncMock(return_value=meta)), \
            patch("bot.admin.backfill_videos._ingest_resolved_video", new=AsyncMock()), \
            caplog.at_level(logging.INFO):
        await amain(dry_run=False, limit=None)

    assert "video download/size check failed" in caplog.text
    assert "done: 0 attached, 0 had no video, 1 failed" in caplog.text


async def test_amain_ingest_exception_counts_failed(global_engine, caplog):
    await _seed_candidate(global_engine)

    meta = ResolvedMetadata(video_url="https://video.twimg.com/clip.mp4")
    p_settings, p_init, p_dispose, p_sleep = _amain_patches()
    with p_settings, p_init, p_dispose, p_sleep, \
            patch("bot.admin.backfill_videos.resolve", new=AsyncMock(return_value=meta)), \
            patch("bot.admin.backfill_videos._ingest_resolved_video",
                  new=AsyncMock(side_effect=OSError("disk full"))), \
            caplog.at_level(logging.INFO):
        await amain(dry_run=False, limit=None)

    assert "video ingest failed" in caplog.text
    assert "done: 0 attached, 0 had no video, 1 failed" in caplog.text


def test_main_parses_argv(monkeypatch):
    amain_mock = MagicMock(return_value="coro-sentinel")
    run_mock = MagicMock()
    monkeypatch.setattr("sys.argv", ["backfill_videos", "--dry-run", "--limit", "5"])
    monkeypatch.setattr("bot.admin.backfill_videos.amain", amain_mock)
    monkeypatch.setattr("bot.admin.backfill_videos.asyncio.run", run_mock)

    main()

    amain_mock.assert_called_once_with(dry_run=True, limit=5)
    run_mock.assert_called_once_with("coro-sentinel")
