"""Tests for resolver-sourced video attachments.

Feature: link-only submissions whose source is a video/GIF post (twitter,
tiktok, reddit GIFs) get the actual video downloaded and attached, so publish
posts native video instead of a thumbnail card. Failures must degrade to the
old thumbnail behavior - never a broken video attachment.

The download now happens in the lockless gather phase (_gather_ingest ->
_download_resolved_video) and the attachment row in the persist phase
(_persist_ingest_outcome); these tests drive that pair (docs/db-lock-io-refactor.md).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from bot.curation.core import _IngestPlan, _LinkPlan, _gather_ingest, _persist_ingest_outcome
from bot.models import Attachment, SubmissionLink
from bot.resolve import ResolvedMetadata
from bot.state import AltTextStatus, SubmissionState

from conftest import make_submission


def _settings():
    s = MagicMock()
    s.attachments_dir = "/tmp/attachments"
    s.data_dir = "/tmp/data"
    s.storage_min_free_mb = 100
    s.youtube_api_key = None
    return s


async def _submission_with_link(session, board, url="https://twitter.com/u/status/1"):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value)
    session.add(sub)
    await session.flush()
    link = SubmissionLink(
        submission_id=sub.id,
        order_index=0,
        raw_url=url,
        canonical_url=url,
        domain_family="twitter",
    )
    session.add(link)
    await session.flush()
    return sub, link


def _video_meta(url="https://video.twimg.com/clip.mp4", width=640, height=480, image_url=None):
    # image_url defaults to None here so the video-download assertions aren't muddied
    # by an incidental thumbnail download (also via download_attachment).
    return ResolvedMetadata(
        title="a tweet", description="Author", image_url=image_url,
        video_url=url, video_width=width, video_height=height, via="fxtwitter_api",
    )


async def _run_link_ingest(session, sub, link, meta, settings, *, has_existing_video=False):
    """Resolve + download one primary link (gather, lockless) and write the results
    (persist), mirroring what ingest_message_content does for a single link."""
    plan = _IngestPlan(
        submission_id=sub.id, board_id=sub.board_id,
        link_plans=[_LinkPlan(link_id=link.id, canonical_url=link.canonical_url,
                              domain_family=link.domain_family, is_primary=True)],
        att_plans=[], embed_title=None, embed_description=None, embed_thumb_url=None,
        thumb_proxy_url=None, has_existing_video=has_existing_video,
    )
    with patch("bot.curation.core.resolve", new_callable=AsyncMock, return_value=meta):
        outcome = await _gather_ingest(plan, settings, AsyncMock())
    await _persist_ingest_outcome(session, outcome, sub.id)


async def _video_attachments(session, submission_id):
    from sqlalchemy import select
    rows = (await session.scalars(
        select(Attachment).where(Attachment.submission_id == submission_id, Attachment.is_video.is_(True))
    )).all()
    return list(rows)


async def test_creates_video_attachment_on_success(session, board, tmp_path):
    sub, link = await _submission_with_link(session, board)
    video_file = tmp_path / "clip.mp4"
    video_file.write_bytes(b"fake-mp4-bytes")

    with patch("bot.curation.core.submission_dir", return_value=str(tmp_path)), \
         patch("bot.curation.core.download_attachment", new_callable=AsyncMock, return_value=str(video_file)) as mock_dl, \
         patch("bot.curation.core._transcode_video", new_callable=AsyncMock, return_value=str(video_file)):
        await _run_link_ingest(session, sub, link, _video_meta(), _settings())

    atts = await _video_attachments(session, sub.id)
    assert len(atts) == 1
    row = atts[0]
    assert row.is_video is True and row.is_image is False
    assert row.local_path == str(video_file)
    assert row.width == 640 and row.height == 480  # noqa: PLR2004
    assert row.mime == "video/mp4"
    assert row.discord_url == "https://video.twimg.com/clip.mp4"
    # Same accessibility policy as Discord-uploaded videos: alt text is prompted
    assert row.alt_text_status == AltTextStatus.NEEDED.value
    mock_dl.assert_awaited_once()


async def test_download_failure_creates_no_row(session, board, tmp_path):
    sub, link = await _submission_with_link(session, board)

    with patch("bot.curation.core.submission_dir", return_value=str(tmp_path)), \
         patch("bot.curation.core.download_attachment", new_callable=AsyncMock,
               side_effect=httpx.ConnectError("cdn unreachable")):
        await _run_link_ingest(session, sub, link, _video_meta(), _settings())

    atts = await _video_attachments(session, sub.id)
    assert atts == [], "failed download must fall back to thumbnail, not create a broken attachment"


async def test_skips_when_video_attachment_already_exists(session, board, tmp_path):
    sub, link = await _submission_with_link(session, board)
    session.add(Attachment(
        submission_id=sub.id, discord_attachment_id=555, filename="existing.mp4",
        discord_url="https://cdn.discord.com/existing.mp4", is_image=False, is_video=True,
        alt_text_status=AltTextStatus.PROVIDED.value, alt_text_body="already here",
    ))
    await session.flush()

    with patch("bot.curation.core.download_attachment", new_callable=AsyncMock) as mock_dl:
        # has_existing_video=True is what _persist_ingest_skeletons computes when a
        # video attachment is already present (Discord-uploaded or preserved video).
        await _run_link_ingest(session, sub, link, _video_meta(), _settings(), has_existing_video=True)

    mock_dl.assert_not_awaited()
    atts = await _video_attachments(session, sub.id)
    assert len(atts) == 1  # only the pre-existing one


async def test_oversize_video_skipped(session, board, tmp_path):
    sub, link = await _submission_with_link(session, board)
    video_file = tmp_path / "big.mp4"
    video_file.write_bytes(b"x" * 64)

    with patch("bot.curation.core.submission_dir", return_value=str(tmp_path)), \
         patch("bot.curation.core.download_attachment", new_callable=AsyncMock, return_value=str(video_file)), \
         patch("bot.curation.core._transcode_video", new_callable=AsyncMock, return_value=str(video_file)), \
         patch("bot.curation.core._MAX_RESOLVED_VIDEO_BYTES", 32):
        await _run_link_ingest(session, sub, link, _video_meta(), _settings())

    atts = await _video_attachments(session, sub.id)
    assert atts == [], "oversize video must fall back to thumbnail (it can never upload)"


async def test_ingest_video_for_primary_link(session, board, tmp_path):
    sub, link = await _submission_with_link(session, board)
    video_file = tmp_path / "clip.mp4"
    video_file.write_bytes(b"fake-mp4-bytes")

    with patch("bot.curation.core.submission_dir", return_value=str(tmp_path)), \
         patch("bot.curation.core.download_attachment", new_callable=AsyncMock, return_value=str(video_file)), \
         patch("bot.curation.core._transcode_video", new_callable=AsyncMock, return_value=str(video_file)):
        await _run_link_ingest(session, sub, link, _video_meta(), _settings())

    atts = await _video_attachments(session, sub.id)
    assert len(atts) == 1
    assert atts[0].local_path == str(video_file)


async def test_no_video_url_no_attachment(session, board, tmp_path):
    sub, link = await _submission_with_link(session, board)
    meta = ResolvedMetadata(title="pic", image_url="https://pbs.twimg.com/photo.jpg", via="fxtwitter_api")

    with patch("bot.curation.core.submission_dir", return_value=str(tmp_path)), \
         patch("bot.curation.core.download_attachment", new_callable=AsyncMock, return_value=str(tmp_path / "thumb")):
        await _run_link_ingest(session, sub, link, meta, _settings())

    atts = await _video_attachments(session, sub.id)
    assert atts == []


# --- stream (reddit) videos: ffmpeg fetches + muxes video+audio --------------


def _stream_meta(url="https://v.redd.it/abc/HLSPlaylist.m3u8", image_url=None):
    return ResolvedMetadata(
        title="reddit vid", description="r/x", image_url=image_url,
        video_url=url, video_width=1920, video_height=1080,
        video_is_stream=True, via="reddit_api",
    )


async def test_stream_video_muxed_and_attached(session, board, tmp_path):
    sub, link = await _submission_with_link(session, board, url="https://www.reddit.com/r/x/comments/1/t/")
    out = tmp_path / f"linkvid_{link.id}.mp4"
    out.write_bytes(b"muxed-mp4")

    with patch("bot.curation.core.submission_dir", return_value=str(tmp_path)), \
         patch("bot.curation.core._fetch_stream_video",
               new_callable=AsyncMock, return_value=str(out)) as mock_mux, \
         patch("bot.curation.core.download_attachment", new_callable=AsyncMock) as mock_dl:
        await _run_link_ingest(session, sub, link, _stream_meta(), _settings())

    mock_mux.assert_awaited_once()  # went through the ffmpeg mux path
    mock_dl.assert_not_awaited()    # not the direct-download path (and no thumbnail)
    atts = await _video_attachments(session, sub.id)
    assert len(atts) == 1 and atts[0].local_path == str(out)


async def test_stream_video_mux_failure_falls_back(session, board, tmp_path):
    sub, link = await _submission_with_link(session, board, url="https://www.reddit.com/r/x/comments/1/t/")
    with patch("bot.curation.core.submission_dir", return_value=str(tmp_path)), \
         patch("bot.curation.core._fetch_stream_video", new_callable=AsyncMock, return_value=None):
        await _run_link_ingest(session, sub, link, _stream_meta(), _settings())
    assert await _video_attachments(session, sub.id) == []  # no broken attachment


async def test_fetch_stream_video_success(tmp_path):
    from bot.curation.core import _fetch_stream_video
    proc = MagicMock()
    proc.returncode = 0
    proc.communicate = AsyncMock(return_value=(b"", b""))
    with patch("bot.curation.core.has_free_space", return_value=True), \
         patch("bot.discord_ingest.service.asyncio.create_subprocess_exec",
               new_callable=AsyncMock, return_value=proc):
        path = await _fetch_stream_video("https://v.redd.it/a/HLSPlaylist.m3u8", str(tmp_path), "out.mp4", _settings())
    assert path == str(tmp_path / "out.mp4")


async def test_fetch_stream_video_ffmpeg_error(tmp_path):
    from bot.curation.core import _fetch_stream_video
    proc = MagicMock()
    proc.returncode = 1
    proc.communicate = AsyncMock(return_value=(b"", b"boom"))
    with patch("bot.curation.core.has_free_space", return_value=True), \
         patch("bot.discord_ingest.service.asyncio.create_subprocess_exec",
               new_callable=AsyncMock, return_value=proc):
        path = await _fetch_stream_video("https://v.redd.it/a/HLSPlaylist.m3u8", str(tmp_path), "out.mp4", _settings())
    assert path is None


async def test_fetch_stream_video_timeout(tmp_path):
    from bot.curation.core import _fetch_stream_video
    proc = MagicMock()
    proc.kill = MagicMock()
    proc.communicate = AsyncMock(side_effect=__import__("asyncio").TimeoutError)
    with patch("bot.curation.core.has_free_space", return_value=True), \
         patch("bot.discord_ingest.service.asyncio.create_subprocess_exec",
               new_callable=AsyncMock, return_value=proc):
        path = await _fetch_stream_video("https://v.redd.it/a/HLSPlaylist.m3u8", str(tmp_path), "out.mp4", _settings())
    assert path is None
    proc.kill.assert_called_once()


async def test_fetch_stream_video_no_space(tmp_path):
    from bot.curation.core import _fetch_stream_video
    with patch("bot.curation.core.has_free_space", return_value=False):
        path = await _fetch_stream_video("https://v.redd.it/a/HLSPlaylist.m3u8", str(tmp_path), "out.mp4", _settings())
    assert path is None


async def test_fetch_stream_video_ffmpeg_missing(tmp_path):
    from bot.curation.core import _fetch_stream_video
    with patch("bot.curation.core.has_free_space", return_value=True), \
         patch("bot.discord_ingest.service.asyncio.create_subprocess_exec",
               new_callable=AsyncMock, side_effect=OSError("ffmpeg not found")):
        path = await _fetch_stream_video("https://v.redd.it/a/HLSPlaylist.m3u8", str(tmp_path), "out.mp4", _settings())
    assert path is None
