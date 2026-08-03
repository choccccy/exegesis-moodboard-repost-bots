"""Characterization tests for the still-Discord-coupled inbound reply helpers
`_ingest_attachment_in_session` and `_resolve_links_in_session` (issue #60).

These run under the DB lock in the supplemental-content reply handlers and are
otherwise mocked out by the handler tests, so they need direct coverage. They
double as the behaviour-preservation backstop for the deferred handler migration.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select

from bot.discord_ingest import service
from bot.curation import core
from bot.curation.surface import NullSurface
from bot.curation.types import InboundAttachment, InboundMessage
from bot.models import Attachment, SubmissionLink
from bot.state import AltTextStatus, SubmissionState

from conftest import bound_session_scope, make_submission

_MISSING_ID = 9_999_999


def _settings():
    s = MagicMock()
    s.attachments_dir = "/tmp/attachments"
    s.data_dir = "/tmp"
    s.storage_min_free_mb = 100
    return s


async def test_ingest_attachment_downloads_and_records_path(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value)
    session.add(sub)
    await session.flush()

    att = InboundAttachment(
        id=555, url="https://cdn/pic.jpg", filename="pic.jpg", content_type="image/jpeg"
    )
    with patch(
        "bot.curation.core._download_attachment_file",
        new=AsyncMock(return_value="/data/attachments/1/1/pic.jpg"),
    ):
        await core._ingest_attachment_in_session(session, sub, att, _settings(), MagicMock())

    rows = list(await session.scalars(
        select(Attachment).where(Attachment.submission_id == sub.id)
    ))
    assert len(rows) == 1
    row = rows[0]
    assert row.discord_attachment_id == 555
    assert row.is_image is True
    assert row.local_path == "/data/attachments/1/1/pic.jpg"
    assert row.downloaded_at is not None
    assert row.alt_text_status == AltTextStatus.NEEDED.value


async def test_ingest_attachment_no_path_when_download_fails(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value)
    session.add(sub)
    await session.flush()

    att = InboundAttachment(
        id=777, url="https://cdn/x.jpg", filename="x.jpg", content_type="image/jpeg"
    )
    with patch(
        "bot.curation.core._download_attachment_file",
        new=AsyncMock(return_value=None),
    ):
        await core._ingest_attachment_in_session(session, sub, att, _settings(), MagicMock())

    row = (await session.scalars(
        select(Attachment).where(Attachment.submission_id == sub.id)
    )).one()
    assert row.local_path is None
    assert row.downloaded_at is None


async def test_resolve_links_reingests_existing_links(session, board):
    sub = make_submission(board, state=SubmissionState.READY_TO_QUEUE.value)
    session.add(sub)
    await session.flush()
    session.add(SubmissionLink(
        submission_id=sub.id, order_index=0, raw_url="https://example.com/a",
        canonical_url="https://example.com/a", domain_family="other",
    ))
    # An existing video exercises the has_existing_video branch.
    session.add(Attachment(
        submission_id=sub.id, discord_attachment_id=9, filename="v.mp4",
        discord_url="u", is_image=False, is_video=True,
        alt_text_status=AltTextStatus.NEEDED.value,
    ))
    await session.flush()

    with (
        patch("bot.curation.core._gather_ingest", new=AsyncMock(return_value=MagicMock())) as gather,
        patch("bot.curation.core._persist_ingest_outcome", new=AsyncMock()) as persist,
    ):
        await core._resolve_links_in_session(session, sub, _settings(), MagicMock())

    gather.assert_awaited_once()
    plan = gather.await_args.args[0]
    assert plan.has_existing_video is True
    assert [lp.canonical_url for lp in plan.link_plans] == ["https://example.com/a"]
    persist.assert_awaited_once()


async def test_resolve_links_noop_when_no_links(session, board):
    sub = make_submission(board, state=SubmissionState.READY_TO_QUEUE.value)
    session.add(sub)
    await session.flush()

    with patch("bot.curation.core._gather_ingest", new=AsyncMock()) as gather:
        await core._resolve_links_in_session(session, sub, _settings(), MagicMock())
    gather.assert_not_awaited()


async def test_persist_ingest_outcome_skips_missing_rows(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value)
    session.add(sub)
    await session.flush()
    # Outcome references an attachment row and a link that no longer exist -
    # both must be skipped rather than crashing.
    outcome = core._IngestOutcome(
        link_outcomes=[core._LinkOutcome(
            link_id=_MISSING_ID, title="t", description="d", image_url=None,
            via="opengraph", source_at_uri=None, image_path=None,
        )],
        att_paths={_MISSING_ID: "/fake/path.jpg"},
    )
    await core._persist_ingest_outcome(session, outcome, sub.id)  # no error


async def test_attach_resolved_video_skips_when_video_exists(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value)
    session.add(sub)
    await session.flush()
    session.add(Attachment(
        submission_id=sub.id, discord_attachment_id=1, filename="v.mp4",
        discord_url="u", is_image=False, is_video=True,
        alt_text_status=AltTextStatus.NEEDED.value,
    ))
    await session.flush()

    created = await core._attach_resolved_video(
        session, sub.id, link_id=1,
        video_url="https://cdn/v.mp4", video_width=1, video_height=1, video_path="/fake/v.mp4",
    )
    assert created is False


# ---------------------------------------------------------------------------
# Race-guard coverage: submission deleted between DB scopes -> graceful bail.
# ---------------------------------------------------------------------------

async def test_ingest_message_content_missing_submission(session):
    with patch("bot.curation.core.session_scope", bound_session_scope(session)):
        # Returns without raising when the submission vanished before ingest.
        await core.ingest_message_content(_settings(), InboundMessage(content="x"), _MISSING_ID, MagicMock())


async def test_reingest_missing_submission(session):
    with patch("bot.discord_ingest.service.session_scope", bound_session_scope(session)):
        await service.reingest_submission(
            _MISSING_ID, message=MagicMock(), settings=_settings(), http_client=MagicMock()
        )


async def test_recompute_missing_submission(session):
    result = await core.recompute_and_request(
        _MISSING_ID, settings=_settings(), destination=NullSurface(),
        yt_client=None, ambient_session=session,
    )
    assert result == SubmissionState.INTENT_SUBMITTED


async def test_publish_queued_missing_submission(session):
    with patch("bot.discord_ingest.service.session_scope", bound_session_scope(session)):
        result = await service.publish_queued_submission(_settings(), _MISSING_ID, NullSurface())
    assert result == service.PublishOutcome.FAILED
