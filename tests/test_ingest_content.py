"""Tests for _capture_embed, _persist_ingest_skeletons, and _download_attachment_file.

These cover the core of the ingestion pipeline, now split (docs/db-lock-io-refactor.md)
into a DB skeleton-persist phase (_persist_ingest_skeletons) and a lockless download
phase (_download_attachment_file), so the platform-agnostic refactor is validated
against unchanged behaviour.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select

from bot.curation.ingest import _AttachmentPlan, _capture_embed, _download_attachment_file, _persist_ingest_skeletons
from bot.curation.types import InboundAttachment, InboundEmbed, InboundMessage, InboundSnapshot
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
# _persist_ingest_skeletons (link rows + embed + attachment skeleton rows, no HTTP)
# ---------------------------------------------------------------------------

async def _skeletons(session, sub, msg):
    return await _persist_ingest_skeletons(session, sub, msg)


@pytest.mark.asyncio
async def test_skeletons_url_from_text(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value)
    session.add(sub)
    await session.flush()

    msg = _message(content="check https://example.com/post out")
    plan = await _skeletons(session, sub, msg)

    links = (await session.scalars(
        select(SubmissionLink).where(SubmissionLink.submission_id == sub.id)
    )).all()
    assert len(links) == 1
    assert links[0].canonical_url == "https://example.com/post"
    assert len(plan.link_plans) == 1
    assert plan.link_plans[0].is_primary is True


@pytest.mark.asyncio
async def test_skeletons_embed_url_fallback(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value)
    session.add(sub)
    await session.flush()

    embed = _embed(url="https://example.com/embed-url")
    msg = _message(content="", embeds=[embed])
    await _skeletons(session, sub, msg)

    links = (await session.scalars(
        select(SubmissionLink).where(SubmissionLink.submission_id == sub.id)
    )).all()
    assert len(links) == 1
    assert links[0].raw_url == "https://example.com/embed-url"


@pytest.mark.asyncio
async def test_skeletons_deduplicates_urls(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value)
    session.add(sub)
    await session.flush()

    embed = _embed(url="https://example.com/post")
    msg = _message(content="https://example.com/post", embeds=[embed])
    await _skeletons(session, sub, msg)

    links = (await session.scalars(
        select(SubmissionLink).where(SubmissionLink.submission_id == sub.id)
    )).all()
    assert len(links) == 1


@pytest.mark.asyncio
async def test_skeletons_creates_attachment_row_and_plan(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value)
    session.add(sub)
    await session.flush()

    att = _attachment(att_id=123, filename="photo.jpg", content_type="image/jpeg")
    msg = _message(content="https://example.com", attachments=[att])
    plan = await _skeletons(session, sub, msg)

    rows = (await session.scalars(
        select(Attachment).where(Attachment.submission_id == sub.id)
    )).all()
    assert len(rows) == 1
    assert rows[0].discord_attachment_id == 123
    assert rows[0].is_image is True
    assert rows[0].local_path is None  # skeleton only - download is the gather phase
    assert len(plan.att_plans) == 1
    assert plan.att_plans[0].url == att.url
    assert plan.att_plans[0].is_video is False


@pytest.mark.asyncio
async def test_skeletons_forwarded_message_snapshot(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value)
    session.add(sub)
    await session.flush()

    snap = InboundSnapshot(content="https://example.com/forwarded")
    msg = _message(content="", snapshots=[snap])
    await _skeletons(session, sub, msg)

    links = (await session.scalars(
        select(SubmissionLink).where(SubmissionLink.submission_id == sub.id)
    )).all()
    assert len(links) == 1
    assert "forwarded" in links[0].raw_url


@pytest.mark.asyncio
async def test_skeletons_captures_embed_proxy_url_into_plan(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value)
    session.add(sub)
    await session.flush()

    embed = _embed(url="https://example.com", title="T", thumb_proxy="https://proxy.example.com/t.jpg")
    msg = _message(content="https://example.com", embeds=[embed])
    plan = await _skeletons(session, sub, msg)

    assert plan.thumb_proxy_url == "https://proxy.example.com/t.jpg"
    assert plan.embed_title == "T"


@pytest.mark.asyncio
async def test_skeletons_has_existing_video_flag(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value)
    session.add(sub)
    await session.flush()

    vid = _attachment(content_type="video/mp4", filename="clip.mp4")
    plan = await _skeletons(session, sub, _message(attachments=[vid]))
    assert plan.has_existing_video is True

    sub2 = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value, source_discord_message_id=2)
    session.add(sub2)
    await session.flush()
    plan2 = await _skeletons(session, sub2, _message(attachments=[_attachment()]))
    assert plan2.has_existing_video is False


# ---------------------------------------------------------------------------
# _download_attachment_file (HTTP only, lockless gather phase)
# ---------------------------------------------------------------------------

def _att_plan(row_id=1, url="https://cdn.discord.com/att.jpg", filename="att.jpg", is_video=False):
    return _AttachmentPlan(row_id=row_id, url=url, filename=filename, is_video=is_video)


@pytest.mark.asyncio
async def test_download_attachment_file_returns_path():
    with patch("bot.curation.ingest.download_attachment",
               new_callable=AsyncMock, return_value="/tmp/dest/1_att.jpg") as dl:
        path = await _download_attachment_file(_att_plan(), "/tmp/dest", _settings(), AsyncMock())
    assert path == "/tmp/dest/1_att.jpg"
    assert dl.call_args.kwargs["filename"] == "1_att.jpg"


@pytest.mark.asyncio
async def test_download_attachment_file_transcodes_video():
    with patch("bot.curation.ingest.download_attachment",
               new_callable=AsyncMock, return_value="/tmp/dest/1_clip.mp4"), \
         patch("bot.curation.ingest._transcode_video",
               new_callable=AsyncMock, return_value="/tmp/dest/1_clip_t.mp4") as tc:
        path = await _download_attachment_file(
            _att_plan(filename="clip.mp4", is_video=True), "/tmp/dest", _settings(), AsyncMock()
        )
    assert path == "/tmp/dest/1_clip_t.mp4"
    tc.assert_awaited_once()


@pytest.mark.asyncio
async def test_download_attachment_file_failure_returns_none():
    import httpx
    with patch("bot.curation.ingest.download_attachment",
               new_callable=AsyncMock, side_effect=httpx.HTTPError("network failure")):
        path = await _download_attachment_file(_att_plan(), "/tmp/dest", _settings(), AsyncMock())
    assert path is None
