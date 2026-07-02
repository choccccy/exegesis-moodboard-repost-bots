"""Tests for _snapshot helper function."""
from __future__ import annotations

from bot.discord_ingest.service import _snapshot
from bot.models import Attachment, MetadataRequest, SubmissionLink
from bot.state import AltTextStatus, GraphicStatus

from conftest import make_submission


async def _add_link(session, submission, url: str, *, order_index: int = 0, resolved_via: str | None = None, resolved_image_path: str | None = None):
    link = SubmissionLink(
        submission_id=submission.id,
        order_index=order_index,
        raw_url=url,
        canonical_url=url,
        domain_family="example",
        resolved_via=resolved_via,
        resolved_image_path=resolved_image_path,
    )
    session.add(link)
    await session.flush()
    return link


async def _add_attachment(session, submission, *, is_image: bool = True, is_video: bool = False, alt_text_status: str = AltTextStatus.PROVIDED.value):
    att = Attachment(
        submission_id=submission.id,
        discord_attachment_id=submission.id * 100 + 1,
        filename="test.jpg",
        discord_url="https://cdn.discord.com/test.jpg",
        is_image=is_image,
        is_video=is_video,
        alt_text_status=alt_text_status,
    )
    session.add(att)
    await session.flush()
    return att


async def test_snapshot_empty_submission(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()

    snap, atts, links = await _snapshot(session, sub)
    assert snap.has_canonical_link is False
    assert atts == []
    assert links == []
    assert snap.has_image is False
    assert snap.needs_image is False


async def test_snapshot_with_link(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    await _add_link(session, sub, "https://example.com/foo", resolved_via="oembed")

    snap, atts, links = await _snapshot(session, sub)
    assert snap.has_canonical_link is True
    assert len(links) == 1
    assert snap.resolved_via == "oembed"


async def test_snapshot_with_uploaded_image(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    await _add_attachment(session, sub, is_image=True, alt_text_status=AltTextStatus.PROVIDED.value)

    snap, atts, links = await _snapshot(session, sub)
    assert snap.has_image is True
    assert len(atts) == 1
    assert AltTextStatus.PROVIDED in snap.image_alt_statuses


async def test_snapshot_alt_text_needed_propagates(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    await _add_attachment(session, sub, is_image=True, alt_text_status=AltTextStatus.NEEDED.value)

    snap, _atts, _links = await _snapshot(session, sub)
    assert AltTextStatus.NEEDED in snap.image_alt_statuses


async def test_snapshot_graphic_status_propagates(session, board):
    sub = make_submission(board, graphic_status=GraphicStatus.GRAPHIC.value)
    session.add(sub)
    await session.flush()

    snap, _atts, _links = await _snapshot(session, sub)
    assert snap.graphic_status == GraphicStatus.GRAPHIC


async def test_snapshot_confirmed_metadata(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    await _add_link(session, sub, "https://example.com/ext", resolved_via=None)
    # Add a confirmed metadata request
    req = MetadataRequest(
        submission_id=sub.id,
        bot_message_id=5000,
        answer="confirmed",
    )
    from datetime import datetime, timezone
    req.answered_at = datetime.now(timezone.utc)
    session.add(req)
    await session.flush()

    snap, _atts, _links = await _snapshot(session, sub)
    assert snap.metadata_confirmed is True


async def test_snapshot_unconfirmed_metadata(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    await _add_link(session, sub, "https://example.com/ext", resolved_via=None)
    req = MetadataRequest(
        submission_id=sub.id,
        bot_message_id=5001,
        answer=None,
    )
    session.add(req)
    await session.flush()

    snap, _atts, _links = await _snapshot(session, sub)
    assert snap.metadata_confirmed is False


async def test_snapshot_has_embed_image_via_resolved_image_path(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    await _add_link(session, sub, "https://example.com/with-thumb", resolved_image_path="/data/thumb.jpg", resolved_via="oembed")

    snap, _atts, _links = await _snapshot(session, sub)
    assert snap.has_image is True


async def test_snapshot_links_returned_in_order(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    await _add_link(session, sub, "https://example.com/b", order_index=1)
    await _add_link(session, sub, "https://example.com/a", order_index=0)

    _snap, _atts, links = await _snapshot(session, sub)
    assert links[0].canonical_url == "https://example.com/a"
    assert links[1].canonical_url == "https://example.com/b"


async def test_snapshot_non_image_attachment_excluded_from_alt_statuses(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    # A non-image, non-video attachment (e.g. a PDF) should not appear in image_alt_statuses
    att = Attachment(
        submission_id=sub.id,
        discord_attachment_id=999,
        filename="file.pdf",
        discord_url="https://cdn.discord.com/file.pdf",
        is_image=False,
        is_video=False,
        alt_text_status=AltTextStatus.NOT_REQUIRED.value,
    )
    session.add(att)
    await session.flush()

    snap, atts, _links = await _snapshot(session, sub)
    assert len(atts) == 1
    assert snap.image_alt_statuses == []
