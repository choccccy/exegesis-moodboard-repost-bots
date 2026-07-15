"""Tests for reingest_submission: re-reading the source message to refresh a
submission in place, preserving curator-entered data.

The submission's source message is re-read (patched via discord_message_to_inbound)
and re-run through _ingest_content + _resolve_links (with resolve/download mocked).
"""

from __future__ import annotations

import itertools
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from bot.discord_ingest import service
from bot.ingest.types import InboundAttachment, InboundMessage
from bot.models import Attachment, SubmissionLink
from bot.resolve import ResolvedMetadata
from bot.state import AltTextStatus, GraphicStatus, SubmissionState

from conftest import make_submission

_ids = itertools.count(90_000)


def _settings():
    s = MagicMock()
    s.attachments_dir = "/tmp/attachments"
    s.data_dir = "/tmp"
    s.storage_min_free_mb = 100
    s.youtube_api_key = None
    return s


def _meta(title="New Title", **kw):
    return ResolvedMetadata(title=title, description="desc", via="opengraph", **kw)


async def _seed(session, board, *, links=None, atts=None, **sub_kw):
    sub = make_submission(board, state=SubmissionState.READY_TO_QUEUE.value, **sub_kw)
    session.add(sub)
    await session.flush()
    for i, url in enumerate(links or []):
        session.add(SubmissionLink(
            submission_id=sub.id, order_index=i, raw_url=url, canonical_url=url,
            domain_family="other", resolved_title="OLD CAPTION",
        ))
    for a in (atts or []):
        session.add(Attachment(submission_id=sub.id, **a))
    await session.flush()
    return sub


async def _reingest(session, sub, inbound, meta=None):
    with (
        patch("bot.discord_ingest.service.discord_message_to_inbound", return_value=inbound),
        patch("bot.discord_ingest.service.resolve", new_callable=AsyncMock, return_value=meta or _meta()),
        patch("bot.discord_ingest.service.download_attachment", new_callable=AsyncMock, return_value="/fake/f.jpg"),
    ):
        await service.reingest_submission(
            session, sub, message=MagicMock(), settings=_settings(), http_client=MagicMock()
        )


async def _links(session, sub):
    return list(await session.scalars(
        select(SubmissionLink).where(SubmissionLink.submission_id == sub.id).order_by(SubmissionLink.order_index)
    ))


async def _atts(session, sub):
    return {a.discord_attachment_id: a for a in await session.scalars(
        select(Attachment).where(Attachment.submission_id == sub.id)
    )}


async def test_reingest_refreshes_caption_from_source(session, board):
    sub = await _seed(session, board, links=["https://example.com/old"])
    inbound = InboundMessage(content="https://example.com/new")
    await _reingest(session, sub, inbound, meta=_meta(title="Fresh Caption"))

    links = await _links(session, sub)
    assert len(links) == 1
    assert "example.com/new" in links[0].canonical_url
    assert links[0].resolved_title == "Fresh Caption"


async def test_reingest_preserves_alt_for_unchanged_attachment(session, board):
    sub = await _seed(session, board, atts=[dict(
        discord_attachment_id=111, filename="pic.jpg", discord_url="https://cdn/pic.jpg",
        is_image=True, is_video=False, alt_text_status=AltTextStatus.PROVIDED.value,
        alt_text_body="a chrome robot", alt_text_author=999,
    )])
    inbound = InboundMessage(attachments=[
        InboundAttachment(id=111, url="https://cdn/pic.jpg", filename="pic.jpg", content_type="image/jpeg"),
    ])
    await _reingest(session, sub, inbound)

    atts = await _atts(session, sub)
    assert set(atts) == {111}
    assert atts[111].alt_text_status == AltTextStatus.PROVIDED.value
    assert atts[111].alt_text_body == "a chrome robot"
    assert atts[111].alt_text_author == 999


async def test_reingest_added_attachment_gets_default_alt(session, board):
    sub = await _seed(session, board, atts=[dict(
        discord_attachment_id=111, filename="a.jpg", discord_url="u", is_image=True,
        alt_text_status=AltTextStatus.PROVIDED.value, alt_text_body="kept",
    )])
    inbound = InboundMessage(attachments=[
        InboundAttachment(id=111, url="https://cdn/a.jpg", filename="a.jpg", content_type="image/jpeg"),
        InboundAttachment(id=222, url="https://cdn/b.jpg", filename="b.jpg", content_type="image/jpeg"),
    ])
    await _reingest(session, sub, inbound)

    atts = await _atts(session, sub)
    assert set(atts) == {111, 222}
    assert atts[111].alt_text_body == "kept"  # preserved
    assert atts[222].alt_text_status == AltTextStatus.NEEDED.value  # new -> default


async def test_reingest_drops_removed_attachment(session, board):
    sub = await _seed(session, board, atts=[
        dict(discord_attachment_id=111, filename="a.jpg", discord_url="u", is_image=True,
             alt_text_status=AltTextStatus.NEEDED.value),
        dict(discord_attachment_id=222, filename="b.jpg", discord_url="u", is_image=True,
             alt_text_status=AltTextStatus.NEEDED.value),
    ])
    inbound = InboundMessage(attachments=[
        InboundAttachment(id=111, url="https://cdn/a.jpg", filename="a.jpg", content_type="image/jpeg"),
    ])
    await _reingest(session, sub, inbound)

    atts = await _atts(session, sub)
    assert set(atts) == {111}


async def test_reingest_changed_links_no_duplicates(session, board):
    sub = await _seed(session, board, links=["https://x/old"])
    inbound = InboundMessage(content="https://a.example/1 https://b.example/2")
    await _reingest(session, sub, inbound)

    links = await _links(session, sub)
    assert len(links) == 2  # noqa: PLR2004
    assert {l.order_index for l in links} == {0, 1}
    assert not any("x/old" in l.canonical_url for l in links)


async def test_reingest_preserves_submission_decisions(session, board):
    sub = await _seed(session, board, links=["https://x/old"], source_waived=True)
    sub.source_note = "Popular Mechanics, 1965"
    sub.source_note_confirmed = True
    sub.graphic_status = GraphicStatus.GRAPHIC.value
    sub.playlist_skipped = True
    await session.flush()

    inbound = InboundMessage(content="https://x/new")
    await _reingest(session, sub, inbound)

    await session.refresh(sub)
    assert sub.source_waived is True
    assert sub.source_note == "Popular Mechanics, 1965"
    assert sub.source_note_confirmed is True
    assert sub.graphic_status == GraphicStatus.GRAPHIC.value
    assert sub.playlist_skipped is True


async def test_reingest_preserves_resolver_video(session, board):
    sub = await _seed(session, board, links=["https://x/old"], atts=[dict(
        discord_attachment_id=0, filename="linkvid_1.mp4", discord_url="https://v.redd.it/x",
        is_image=False, is_video=True, alt_text_status=AltTextStatus.PROVIDED.value,
        alt_text_body="the clip", local_path="/vol/v.mp4",
    )])
    inbound = InboundMessage(content="https://x/new")
    await _reingest(session, sub, inbound)

    vids = [a for a in (await _atts(session, sub)).values() if a.is_video]
    assert len(vids) == 1  # not duplicated
    assert vids[0].discord_attachment_id == 0
    assert vids[0].alt_text_body == "the clip"  # preserved
    assert vids[0].local_path == "/vol/v.mp4"


async def test_reingest_fresh_description_replaces_default_alt(session, board):
    # A NEEDED (default) alt is NOT preserved; a fresh Discord description wins.
    sub = await _seed(session, board, atts=[dict(
        discord_attachment_id=111, filename="a.jpg", discord_url="u", is_image=True,
        alt_text_status=AltTextStatus.NEEDED.value, alt_text_body=None,
    )])
    inbound = InboundMessage(attachments=[
        InboundAttachment(id=111, url="https://cdn/a.jpg", filename="a.jpg",
                          content_type="image/jpeg", description="fresh caption from discord"),
    ])
    await _reingest(session, sub, inbound)

    atts = await _atts(session, sub)
    assert atts[111].alt_text_status == AltTextStatus.PROVIDED.value
    assert atts[111].alt_text_body == "fresh caption from discord"
