"""Tests for _capture_embed, _ingest_content, and _ingest_attachment.

These cover the zero-tested core of the ingestion pipeline so that the
platform-agnostic refactor can be validated against unchanged behaviour.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select

from bot.discord_ingest.service import (
    _capture_embed,
    _ingest_attachment,
    _ingest_content,
)
from bot.ingest.types import InboundAttachment, InboundEmbed, InboundMessage, InboundSnapshot
from bot.models import Attachment, SubmissionLink
from bot.state import SubmissionState

from conftest import make_submission


# ---------------------------------------------------------------------------
# Inbound type helpers
# ---------------------------------------------------------------------------

def _embed(
    url: str | None = None,
    title: str | None = None,
    description: str | None = None,
    thumb_url: str | None = None,
    thumb_proxy: str | None = None,
    image_url: str | None = None,
    image_proxy: str | None = None,
    author_name: str | None = None,
) -> InboundEmbed:
    return InboundEmbed(
        url=url,
        title=title,
        description=description,
        thumbnail_url=thumb_url,
        thumbnail_proxy_url=thumb_proxy,
        image_url=image_url,
        image_proxy_url=image_proxy,
        author_name=author_name,
    )


def _message(
    content: str = "",
    embeds: list = [],
    attachments: list = [],
    snapshots: list = [],
) -> InboundMessage:
    return InboundMessage(
        content=content,
        embeds=list(embeds),
        attachments=list(attachments),
        snapshots=list(snapshots),
    )


def _attachment(
    att_id: int = 1,
    url: str = "https://cdn.discord.com/att.jpg",
    proxy_url: str = "https://proxy.discord.com/att.jpg",
    content_type: str = "image/jpeg",
    filename: str = "att.jpg",
    description: str | None = None,
    width: int = 100,
    height: int = 100,
) -> InboundAttachment:
    return InboundAttachment(
        id=att_id,
        url=url,
        proxy_url=proxy_url,
        content_type=content_type,
        filename=filename,
        description=description,
        width=width,
        height=height,
        spoiler=False,
    )


def _settings():
    s = MagicMock()
    s.attachments_dir = "/tmp/attachments"
    s.data_dir = "/tmp/data"
    s.storage_min_free_mb = 100
    return s


# ---------------------------------------------------------------------------
# _capture_embed
# ---------------------------------------------------------------------------

def test_capture_embed_sets_title_and_description(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value)
    embed = _embed(title="Hello", description="World", thumb_url="https://t.jpg", thumb_proxy="https://p.jpg")
    msg = _message(embeds=[embed])

    _capture_embed(sub, msg)
    assert sub.embed_title == "Hello"
    assert sub.embed_description == "World"
    assert sub.embed_thumb_url == "https://t.jpg"


def test_capture_embed_returns_thumbnail_proxy(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value)
    embed = _embed(title="T", thumb_url="https://t.jpg", thumb_proxy="https://proxy.jpg")
    msg = _message(embeds=[embed])

    proxy = _capture_embed(sub, msg)
    assert proxy == "https://proxy.jpg"


def test_capture_embed_image_fallback_when_no_thumbnail(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value)
    embed = _embed(title="T", image_url="https://img.jpg", image_proxy="https://img-proxy.jpg")
    msg = _message(embeds=[embed])

    proxy = _capture_embed(sub, msg)
    assert sub.embed_thumb_url == "https://img.jpg"
    assert proxy == "https://img-proxy.jpg"


def test_capture_embed_no_embeds_returns_none(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value)
    msg = _message(embeds=[])
    result = _capture_embed(sub, msg)
    assert result is None
    assert sub.embed_title is None


def test_capture_embed_skips_embed_with_no_content(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value)
    # Embed has no title, description, or image - should be skipped
    embed = _embed(url="https://example.com")
    msg = _message(embeds=[embed])
    result = _capture_embed(sub, msg)
    assert result is None
    assert sub.embed_title is None


def test_capture_embed_reads_message_snapshots(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value)
    snap_embed = _embed(title="Snap Title", thumb_url="https://snap.jpg", thumb_proxy="https://snap-p.jpg")
    snap = InboundSnapshot(embeds=[snap_embed])
    msg = _message(snapshots=[snap])

    proxy = _capture_embed(sub, msg)
    assert sub.embed_title == "Snap Title"
    assert proxy == "https://snap-p.jpg"


# ---------------------------------------------------------------------------
# _ingest_content
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ingest_content_url_from_text(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value)
    session.add(sub)
    await session.flush()

    msg = _message(content="check https://example.com/post out")
    with patch("bot.discord_ingest.service._ingest_attachment", new_callable=AsyncMock):
        await _ingest_content(session, sub, msg, _settings(), AsyncMock())

    links = (await session.scalars(
        select(SubmissionLink).where(SubmissionLink.submission_id == sub.id)
    )).all()
    assert len(links) == 1
    assert links[0].canonical_url == "https://example.com/post"


@pytest.mark.asyncio
async def test_ingest_content_embed_url_fallback(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value)
    session.add(sub)
    await session.flush()

    embed = _embed(url="https://example.com/embed-url")
    msg = _message(content="", embeds=[embed])
    with patch("bot.discord_ingest.service._ingest_attachment", new_callable=AsyncMock):
        await _ingest_content(session, sub, msg, _settings(), AsyncMock())

    links = (await session.scalars(
        select(SubmissionLink).where(SubmissionLink.submission_id == sub.id)
    )).all()
    assert len(links) == 1
    assert links[0].raw_url == "https://example.com/embed-url"


@pytest.mark.asyncio
async def test_ingest_content_deduplicates_urls(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value)
    session.add(sub)
    await session.flush()

    embed = _embed(url="https://example.com/post")
    msg = _message(content="https://example.com/post", embeds=[embed])
    with patch("bot.discord_ingest.service._ingest_attachment", new_callable=AsyncMock):
        await _ingest_content(session, sub, msg, _settings(), AsyncMock())

    links = (await session.scalars(
        select(SubmissionLink).where(SubmissionLink.submission_id == sub.id)
    )).all()
    assert len(links) == 1


@pytest.mark.asyncio
async def test_ingest_content_calls_ingest_attachment(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value)
    session.add(sub)
    await session.flush()

    att = _attachment()
    msg = _message(content="https://example.com", attachments=[att])
    with patch("bot.discord_ingest.service._ingest_attachment", new_callable=AsyncMock) as mock_ingest:
        await _ingest_content(session, sub, msg, _settings(), AsyncMock())

    mock_ingest.assert_awaited_once()
    assert mock_ingest.call_args.args[2] is att


@pytest.mark.asyncio
async def test_ingest_content_forwarded_message_snapshot(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value)
    session.add(sub)
    await session.flush()

    snap = InboundSnapshot(content="https://example.com/forwarded")
    msg = _message(content="", snapshots=[snap])
    with patch("bot.discord_ingest.service._ingest_attachment", new_callable=AsyncMock):
        await _ingest_content(session, sub, msg, _settings(), AsyncMock())

    links = (await session.scalars(
        select(SubmissionLink).where(SubmissionLink.submission_id == sub.id)
    )).all()
    assert len(links) == 1
    assert "forwarded" in links[0].raw_url


@pytest.mark.asyncio
async def test_ingest_content_returns_proxy_url(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value)
    session.add(sub)
    await session.flush()

    embed = _embed(url="https://example.com", title="T", thumb_proxy="https://proxy.example.com/t.jpg")
    msg = _message(content="https://example.com", embeds=[embed])
    with patch("bot.discord_ingest.service._ingest_attachment", new_callable=AsyncMock):
        result = await _ingest_content(session, sub, msg, _settings(), AsyncMock())

    assert result == "https://proxy.example.com/t.jpg"


# ---------------------------------------------------------------------------
# _ingest_attachment
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ingest_attachment_creates_row(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value)
    session.add(sub)
    await session.flush()

    att = _attachment(att_id=123, filename="photo.jpg", content_type="image/jpeg")
    with patch("bot.discord_ingest.service.submission_dir", return_value="/tmp/dest"), \
         patch("bot.discord_ingest.service.download_attachment", new_callable=AsyncMock, return_value="/tmp/dest/1_photo.jpg"):
        row = await _ingest_attachment(session, sub, att, _settings(), AsyncMock())

    assert row.discord_attachment_id == 123
    assert row.filename == "photo.jpg"
    assert row.mime == "image/jpeg"
    assert row.submission_id == sub.id


def test_ingest_attachment_image_flag(session, board):
    pass  # tested via creates_row + is_image assertions below


@pytest.mark.asyncio
async def test_ingest_attachment_is_image(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value)
    session.add(sub)
    await session.flush()

    att = _attachment(content_type="image/png", filename="img.png")
    with patch("bot.discord_ingest.service.submission_dir", return_value="/tmp/dest"), \
         patch("bot.discord_ingest.service.download_attachment", new_callable=AsyncMock, return_value="/tmp/dest/1_img.png"):
        row = await _ingest_attachment(session, sub, att, _settings(), AsyncMock())

    assert row.is_image is True
    assert row.is_video is False


@pytest.mark.asyncio
async def test_ingest_attachment_is_video(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value)
    session.add(sub)
    await session.flush()

    att = _attachment(content_type="video/mp4", filename="clip.mp4")
    with patch("bot.discord_ingest.service.submission_dir", return_value="/tmp/dest"), \
         patch("bot.discord_ingest.service.download_attachment", new_callable=AsyncMock, return_value="/tmp/dest/1_clip.mp4"), \
         patch("bot.discord_ingest.service._transcode_video", new_callable=AsyncMock, return_value="/tmp/dest/1_clip_transcoded.mp4"):
        row = await _ingest_attachment(session, sub, att, _settings(), AsyncMock())

    assert row.is_video is True
    assert row.is_image is False


@pytest.mark.asyncio
async def test_ingest_attachment_description_stored(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value)
    session.add(sub)
    await session.flush()

    att = _attachment(description="A nice photo of a robot")
    with patch("bot.discord_ingest.service.submission_dir", return_value="/tmp/dest"), \
         patch("bot.discord_ingest.service.download_attachment", new_callable=AsyncMock, return_value="/tmp/dest/1_att.jpg"):
        row = await _ingest_attachment(session, sub, att, _settings(), AsyncMock())

    assert row.alt_text_body == "A nice photo of a robot"


@pytest.mark.asyncio
async def test_ingest_attachment_no_description(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value)
    session.add(sub)
    await session.flush()

    att = _attachment(description=None)
    with patch("bot.discord_ingest.service.submission_dir", return_value="/tmp/dest"), \
         patch("bot.discord_ingest.service.download_attachment", new_callable=AsyncMock, return_value="/tmp/dest/1_att.jpg"):
        row = await _ingest_attachment(session, sub, att, _settings(), AsyncMock())

    assert not row.alt_text_body


@pytest.mark.asyncio
async def test_ingest_attachment_local_path_set(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value)
    session.add(sub)
    await session.flush()

    att = _attachment()
    with patch("bot.discord_ingest.service.submission_dir", return_value="/tmp/dest"), \
         patch("bot.discord_ingest.service.download_attachment", new_callable=AsyncMock, return_value="/tmp/dest/1_att.jpg"):
        row = await _ingest_attachment(session, sub, att, _settings(), AsyncMock())

    assert row.local_path == "/tmp/dest/1_att.jpg"


@pytest.mark.asyncio
async def test_ingest_attachment_download_failure_doesnt_raise(session, board):
    """Network errors are caught and logged; row is still returned."""
    import httpx
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value)
    session.add(sub)
    await session.flush()

    att = _attachment()
    with patch("bot.discord_ingest.service.submission_dir", return_value="/tmp/dest"), \
         patch("bot.discord_ingest.service.download_attachment",
               new_callable=AsyncMock,
               side_effect=httpx.HTTPError("network failure")):
        row = await _ingest_attachment(session, sub, att, _settings(), AsyncMock())

    assert row is not None
    assert row.local_path is None
