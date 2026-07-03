"""Tests for _ingest_resolved_video: resolver-sourced video attachments.

Feature: link-only submissions whose source is a video/GIF post (twitter,
tiktok, reddit GIFs) get the actual video downloaded and attached, so publish
posts native video instead of a thumbnail card. Failures must degrade to the
old thumbnail behavior - never a broken video attachment.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from bot.discord_ingest.service import _ingest_resolved_video, _resolve_links
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


def _video_meta(url="https://video.twimg.com/clip.mp4", width=640, height=480):
    return ResolvedMetadata(
        title="a tweet", description="Author", image_url="https://pbs.twimg.com/still.jpg",
        video_url=url, video_width=width, video_height=height, via="fxtwitter_api",
    )


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

    with patch("bot.discord_ingest.service.submission_dir", return_value=str(tmp_path)), \
         patch("bot.discord_ingest.service.download_attachment", new_callable=AsyncMock, return_value=str(video_file)) as mock_dl, \
         patch("bot.discord_ingest.service._transcode_video", new_callable=AsyncMock, return_value=str(video_file)):
        await _ingest_resolved_video(session, sub, link, _video_meta(), _settings(), AsyncMock())

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

    with patch("bot.discord_ingest.service.submission_dir", return_value=str(tmp_path)), \
         patch("bot.discord_ingest.service.download_attachment", new_callable=AsyncMock,
               side_effect=httpx.ConnectError("cdn unreachable")):
        await _ingest_resolved_video(session, sub, link, _video_meta(), _settings(), AsyncMock())

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

    with patch("bot.discord_ingest.service.download_attachment", new_callable=AsyncMock) as mock_dl:
        await _ingest_resolved_video(session, sub, link, _video_meta(), _settings(), AsyncMock())

    mock_dl.assert_not_awaited()
    atts = await _video_attachments(session, sub.id)
    assert len(atts) == 1  # only the pre-existing one


async def test_oversize_video_skipped(session, board, tmp_path):
    sub, link = await _submission_with_link(session, board)
    video_file = tmp_path / "big.mp4"
    video_file.write_bytes(b"x" * 64)

    with patch("bot.discord_ingest.service.submission_dir", return_value=str(tmp_path)), \
         patch("bot.discord_ingest.service.download_attachment", new_callable=AsyncMock, return_value=str(video_file)), \
         patch("bot.discord_ingest.service._transcode_video", new_callable=AsyncMock, return_value=str(video_file)), \
         patch("bot.discord_ingest.service._MAX_RESOLVED_VIDEO_BYTES", 32):
        await _ingest_resolved_video(session, sub, link, _video_meta(), _settings(), AsyncMock())

    atts = await _video_attachments(session, sub.id)
    assert atts == [], "oversize video must fall back to thumbnail (it can never upload)"


async def test_resolve_links_ingests_video_for_primary_link(session, board, tmp_path):
    sub, link = await _submission_with_link(session, board)
    video_file = tmp_path / "clip.mp4"
    video_file.write_bytes(b"fake-mp4-bytes")

    with patch("bot.discord_ingest.service.resolve", new_callable=AsyncMock, return_value=_video_meta()), \
         patch("bot.discord_ingest.service.submission_dir", return_value=str(tmp_path)), \
         patch("bot.discord_ingest.service.download_attachment", new_callable=AsyncMock, return_value=str(video_file)), \
         patch("bot.discord_ingest.service._transcode_video", new_callable=AsyncMock, return_value=str(video_file)):
        await _resolve_links(session, sub, _settings(), AsyncMock())

    atts = await _video_attachments(session, sub.id)
    assert len(atts) == 1
    assert atts[0].local_path == str(video_file)


async def test_resolve_links_no_video_url_no_attachment(session, board, tmp_path):
    sub, link = await _submission_with_link(session, board)
    meta = ResolvedMetadata(title="pic", image_url="https://pbs.twimg.com/photo.jpg", via="fxtwitter_api")

    with patch("bot.discord_ingest.service.resolve", new_callable=AsyncMock, return_value=meta), \
         patch("bot.discord_ingest.service.submission_dir", return_value=str(tmp_path)), \
         patch("bot.discord_ingest.service.download_attachment", new_callable=AsyncMock, return_value=str(tmp_path / "thumb")):
        await _resolve_links(session, sub, _settings(), AsyncMock())

    atts = await _video_attachments(session, sub.id)
    assert atts == []
