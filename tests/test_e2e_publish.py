"""End-to-end tests: Discord message in → Bluesky post out.

These tests run through the real ingestion and publish code with only three
things mocked out:
  - The Discord API (thread creation, message send, forward)
  - Link resolution HTTP calls (_resolve_links patched to a no-op, then we
    write resolved metadata directly to the DB)
  - The Bluesky API (publisher.publish_submission)

Everything in between — _ingest_content, _capture_embed, _ingest_attachment,
_ensure_thread, recompute_and_request, _snapshot, _attempt_publish — runs real
code against the real in-memory SQLite DB.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from sqlalchemy import select

from bot.config import BoardConfig
from bot.discord_ingest.service import handle_reaction, publish_queued_submission
from bot.models import Attachment, PublishAttempt, Submission, SubmissionLink
from bot.publish import PublishResult
from bot.state import PublishOutcome, SubmissionState

from conftest import MockDest, make_submission

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_send_id = itertools.count(10_000)


def _board_cfg(board) -> BoardConfig:
    return BoardConfig(
        name=board.name,
        discord_guild_id=board.discord_guild_id,
        discord_channel_id=board.discord_channel_id,
        bluesky_handle=f"@{board.name}.exegesis.space",
        require_graphic_classification=False,
        tags=[],
    )


def _settings(board) -> MagicMock:
    cfg = _board_cfg(board)
    s = MagicMock()
    s.board_for_channel.return_value = cfg
    s.bsky_password_for.return_value = "test-app-password"
    s.trigger_emoji = "🦋"
    s.dashboard_url = None
    s.attachments_dir = "/tmp/test-e2e-atts"
    s.data_dir = "/tmp/test-e2e-data"
    s.storage_min_free_mb = 0
    s.youtube_api_key = None
    return s


async def _make_thread_send(*args, **kwargs) -> MagicMock:
    m = MagicMock()
    m.id = next(_send_id)
    m.add_reaction = AsyncMock()
    return m


def _discord_message(
    board,
    *,
    msg_id: int = 42,
    content: str = "https://example.com/cool-robot",
    discord_attachments: list | None = None,
) -> tuple[MagicMock, MagicMock]:
    """Return (message, thread) mocks.

    The thread is what create_thread() will return — its `send` mock uses a
    counter so every call gets a unique `.id`, avoiding bot_message_id clashes.
    """
    thread = MagicMock(spec=discord.Thread)
    thread.id = 500
    thread.archived = False
    thread.send = _make_thread_send
    thread.edit = AsyncMock()

    msg = MagicMock(spec=discord.Message)
    msg.id = msg_id
    msg.content = content
    msg.embeds = []
    msg.attachments = discord_attachments or []
    msg.message_snapshots = []
    msg.created_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
    msg.reference = None

    author = MagicMock()
    author.id = 999
    author.display_name = "test_user"
    msg.author = author

    channel = MagicMock()
    channel.id = board.discord_channel_id
    channel.create_thread = AsyncMock(return_value=thread)
    msg.channel = channel

    guild = MagicMock()
    guild.id = 1
    guild.get_thread = MagicMock(return_value=None)
    guild.fetch_channel = AsyncMock(side_effect=discord.NotFound(MagicMock(), "not found"))
    msg.guild = guild

    msg.forward = AsyncMock()
    return msg, thread


def _discord_attachment(
    att_id: int = 1,
    filename: str = "robot.jpg",
    content_type: str = "image/jpeg",
    url: str = "https://cdn.discord.com/robot.jpg",
    description: str | None = None,
) -> MagicMock:
    a = MagicMock(spec=discord.Attachment)
    a.id = att_id
    a.url = url
    a.proxy_url = f"https://proxy.discord.com/{filename}"
    a.content_type = content_type
    a.filename = filename
    a.description = description
    a.width = 800
    a.height = 600
    a.is_spoiler = MagicMock(return_value=False)
    return a


_OK_RESULT = PublishResult(
    success=True,
    at_uri="at://did:plc:abc123/app.bsky.feed.post/xyz789",
    at_cid="bafyreiabc",
    bsky_url="https://bsky.app/profile/robots.exegesis.space/post/xyz789",
)

_FAIL_RESULT = PublishResult(
    success=False,
    error="rate limited by Bluesky",
)


# ---------------------------------------------------------------------------
# Test 1: external link post (URL only, no attachment)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e2e_external_link_published(session, board):
    """A Discord message with a URL flows to a successful Bluesky external post."""
    settings = _settings(board)
    msg, _ = _discord_message(board, content="https://example.com/cool-robot")

    with patch("bot.discord_ingest.service._resolve_links", new_callable=AsyncMock), \
         patch("bot.publish.publish_submission", new_callable=AsyncMock, return_value=_OK_RESULT) as mock_pub:

        # Step 1: ingest
        await handle_reaction(
            session, settings=settings, message=msg,
            http_client=AsyncMock(), skip_auth=True,
        )

        # Step 2: simulate link resolution (normally done by _resolve_links over HTTP)
        link = await session.scalar(
            select(SubmissionLink)
            .join(Submission, SubmissionLink.submission_id == Submission.id)
            .where(Submission.board_id == board.id)
        )
        assert link is not None, "SubmissionLink should have been created from message URL"
        link.resolved_title = "Cool Robot Post"
        link.resolved_image_url = "https://example.com/thumb.jpg"
        link.resolved_image_path = "/tmp/thumb.jpg"
        link.resolved_via = "opengraph"
        await session.flush()

        # Step 3: simulate curator approving (real path goes via handle_confirm_button
        # which needs a discord.Interaction; bypass here to keep test focused on data flow)
        submission = await session.scalar(select(Submission).where(Submission.board_id == board.id))
        assert submission is not None
        submission.state = SubmissionState.QUEUED.value
        await session.flush()

        # Step 4: publish
        dest = MockDest()
        published = await publish_queued_submission(session, settings, submission, dest)

    # --- assertions ---
    assert published is PublishOutcome.PUBLISHED

    # Bluesky was called once with the right board and password
    mock_pub.assert_awaited_once()
    call_kwargs = mock_pub.call_args.kwargs
    assert call_kwargs["board_cfg"].bluesky_handle == f"@{board.name}.exegesis.space"
    assert call_kwargs["password"] == "test-app-password"

    # The right link data was passed
    assert len(call_kwargs["links"]) == 1
    assert call_kwargs["links"][0].raw_url == "https://example.com/cool-robot"
    assert call_kwargs["links"][0].resolved_title == "Cool Robot Post"

    # No attachments for a URL-only post
    assert call_kwargs["attachments"] == []

    # DB state is correct (object mutated in-place; PublishAttempt query autoflushes first)
    attempt = await session.scalar(
        select(PublishAttempt).where(PublishAttempt.submission_id == submission.id)
    )
    assert attempt is not None
    assert attempt.success is True
    assert attempt.at_uri == _OK_RESULT.at_uri
    assert attempt.bsky_url == _OK_RESULT.bsky_url
    assert submission.state == SubmissionState.PUBLISHED.value

    # Discord thread received a published notice containing the Bluesky URL
    assert any("bsky.app" in m for m in dest.sent), f"no bsky.app URL in notices: {dest.sent}"
    # Thread was archived after publish
    assert any("[archive]" in m for m in dest.sent), f"thread not archived: {dest.sent}"


# ---------------------------------------------------------------------------
# Test 2: image attachment post
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e2e_image_attachment_published(session, board):
    """A Discord message with an image attachment flows to a Bluesky images post."""
    settings = _settings(board)
    att = _discord_attachment(att_id=77, filename="robot.jpg", description="A chrome robot")
    msg, _ = _discord_message(
        board,
        content="https://example.com/source",
        discord_attachments=[att],
    )

    with patch("bot.discord_ingest.service._resolve_links", new_callable=AsyncMock), \
         patch("bot.discord_ingest.service.submission_dir", return_value="/tmp/e2e-atts"), \
         patch("bot.discord_ingest.service.download_attachment", new_callable=AsyncMock,
               return_value="/tmp/e2e-atts/1_robot.jpg"), \
         patch("bot.publish.publish_submission", new_callable=AsyncMock, return_value=_OK_RESULT) as mock_pub:

        await handle_reaction(
            session, settings=settings, message=msg,
            http_client=AsyncMock(), skip_auth=True,
        )

        # Verify attachment row was created
        db_att = await session.scalar(
            select(Attachment)
            .join(Submission, Attachment.submission_id == Submission.id)
            .where(Submission.board_id == board.id)
        )
        assert db_att is not None, "Attachment row should exist"
        assert db_att.discord_attachment_id == 77
        assert db_att.is_image is True
        assert db_att.alt_text_body == "A chrome robot"
        assert db_att.local_path == "/tmp/e2e-atts/1_robot.jpg"

        submission = await session.scalar(select(Submission).where(Submission.board_id == board.id))
        submission.state = SubmissionState.QUEUED.value
        await session.flush()

        dest = MockDest()
        published = await publish_queued_submission(session, settings, submission, dest)

    assert published is PublishOutcome.PUBLISHED
    mock_pub.assert_awaited_once()
    call_kwargs = mock_pub.call_args.kwargs

    # One link and one attachment passed to Bluesky
    assert len(call_kwargs["links"]) == 1
    assert len(call_kwargs["attachments"]) == 1
    assert call_kwargs["attachments"][0].is_image is True
    assert call_kwargs["attachments"][0].alt_text_body == "A chrome robot"

    assert submission.state == SubmissionState.PUBLISHED.value


# ---------------------------------------------------------------------------
# Test 3: Bluesky publish failure is recorded correctly
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e2e_publish_failure_recorded(session, board):
    """When Bluesky returns an error, state becomes PUBLISH_FAILED and the error is saved."""
    settings = _settings(board)
    msg, _ = _discord_message(board)

    with patch("bot.discord_ingest.service._resolve_links", new_callable=AsyncMock), \
         patch("bot.publish.publish_submission", new_callable=AsyncMock, return_value=_FAIL_RESULT):

        await handle_reaction(
            session, settings=settings, message=msg,
            http_client=AsyncMock(), skip_auth=True,
        )

        submission = await session.scalar(select(Submission).where(Submission.board_id == board.id))
        submission.state = SubmissionState.QUEUED.value
        await session.flush()

        dest = MockDest()
        published = await publish_queued_submission(session, settings, submission, dest)

    assert published is PublishOutcome.FAILED  # real attempt was made and failed

    attempt = await session.scalar(
        select(PublishAttempt).where(PublishAttempt.submission_id == submission.id)
    )
    assert attempt is not None
    assert attempt.success is False
    assert "rate limited" in (attempt.error or "")
    assert submission.state == SubmissionState.PUBLISH_FAILED.value

    # Discord thread received a failure notice
    assert any("failed" in m.lower() or "error" in m.lower() for m in dest.sent), \
        f"no failure notice in thread messages: {dest.sent}"


# ---------------------------------------------------------------------------
# Test 4: adapter boundary - verify InboundMessage fields reach the DB
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e2e_embed_metadata_captured(session, board):
    """Embed title and thumbnail from the Discord message reach submission.embed_* fields."""
    settings = _settings(board)

    embed = MagicMock(spec=discord.Embed)
    embed.url = "https://example.com/post"
    embed.title = "The Embed Title"
    embed.description = "The embed description"
    embed.thumbnail = MagicMock()
    embed.thumbnail.url = "https://example.com/thumb.jpg"
    embed.thumbnail.proxy_url = "https://proxy.example.com/thumb.jpg"
    embed.image = None
    embed.author = None

    msg, _ = _discord_message(board, content="https://example.com/post")
    msg.embeds = [embed]

    with patch("bot.discord_ingest.service._resolve_links", new_callable=AsyncMock):
        await handle_reaction(
            session, settings=settings, message=msg,
            http_client=AsyncMock(), skip_auth=True,
        )

    submission = await session.scalar(select(Submission).where(Submission.board_id == board.id))
    assert submission is not None
    assert submission.embed_title == "The Embed Title"
    assert submission.embed_description == "The embed description"
    assert submission.embed_thumb_url == "https://example.com/thumb.jpg"


# ---------------------------------------------------------------------------
# Test 5: duplicate detection aborts publish
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e2e_duplicate_url_aborts_publish(session, board):
    """If the URL was already published by another submission, publish is skipped."""
    settings = _settings(board)

    # First submission: already published
    sub1 = make_submission(board, state=SubmissionState.PUBLISHED.value, source_discord_message_id=10)
    session.add(sub1)
    await session.flush()
    link1 = SubmissionLink(
        submission_id=sub1.id,
        order_index=0,
        raw_url="https://example.com/already-posted",
        canonical_url="https://example.com/already-posted",
        domain_family="other",
    )
    session.add(link1)
    await session.flush()
    prior_attempt = PublishAttempt(
        submission_id=sub1.id,
        success=True,
        at_uri="at://did:plc:old/post/old",
        at_cid="oldcid",
        bsky_url="https://bsky.app/profile/robots.exegesis.space/post/old",
    )
    session.add(prior_attempt)
    await session.flush()

    # Second submission: same URL, now in QUEUED state
    sub2 = make_submission(
        board,
        state=SubmissionState.QUEUED.value,
        source_discord_message_id=11,
        channel_id=board.discord_channel_id,
    )
    session.add(sub2)
    await session.flush()
    link2 = SubmissionLink(
        submission_id=sub2.id,
        order_index=0,
        raw_url="https://example.com/already-posted",
        canonical_url="https://example.com/already-posted",
        domain_family="other",
    )
    session.add(link2)
    await session.flush()

    dest = MockDest()
    with patch("bot.publish.publish_submission", new_callable=AsyncMock) as mock_pub:
        result = await publish_queued_submission(session, settings, sub2, dest)

    # Bluesky should NOT have been called - duplicate detected before publish
    assert result is PublishOutcome.DUPLICATE, "duplicate cleanup lets the scheduler continue to next"
    mock_pub.assert_not_awaited()

    attempt = await session.scalar(
        select(PublishAttempt).where(PublishAttempt.submission_id == sub2.id)
    )
    # State should be PUBLISHED (not PUBLISH_FAILED) - duplicate is silently accepted
    assert sub2.state == SubmissionState.PUBLISHED.value

    assert attempt is not None
    assert attempt.success is True
    assert "duplicate" in (attempt.error or "")
