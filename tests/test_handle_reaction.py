"""Tests for handle_reaction thread-creation and rate-limit resilience.

Key invariant: the existing-thread path in _ensure_thread never calls
create_thread, so a thread-creation rate limit on one submission does NOT
block message sends in already-open threads for other submissions.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from sqlalchemy import select

from bot.discord_ingest.service import handle_reaction
from bot.models import Submission, SubmissionLink, SubmissionThread
from bot.state import SubmissionState

from conftest import make_submission


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _message(channel_id: int, msg_id: int = 42, author_id: int = 999) -> MagicMock:
    msg = MagicMock(spec=discord.Message)
    msg.id = msg_id
    msg.content = ""
    msg.embeds = []
    msg.attachments = []
    msg.message_snapshots = []
    msg.created_at = datetime(2024, 1, 1, tzinfo=timezone.utc)

    author = MagicMock()
    author.id = author_id
    author.display_name = "testuser"
    msg.author = author

    channel = MagicMock()
    channel.id = channel_id
    channel.create_thread = AsyncMock()
    msg.channel = channel

    guild = MagicMock()
    guild.id = 1
    guild.get_thread = MagicMock(return_value=None)
    guild.fetch_channel = AsyncMock(side_effect=discord.NotFound(MagicMock(), "not found"))
    msg.guild = guild

    msg.forward = AsyncMock()
    msg.reference = None
    return msg


def _thread(thread_id: int = 500) -> MagicMock:
    t = MagicMock(spec=discord.Thread)
    t.id = thread_id
    t.archived = False
    t.send = AsyncMock(return_value=MagicMock(id=9999, add_reaction=AsyncMock()))
    t.edit = AsyncMock()
    t.guild = MagicMock()
    return t


def _settings() -> MagicMock:
    s = MagicMock()
    cfg = MagicMock()
    cfg.youtube_playlist_id = None
    cfg.bluesky_handle = "@robots.exegesis.space"
    cfg.display_name = "Robots"
    cfg.require_graphic_classification = False
    s.board_for_channel.return_value = cfg
    s.dashboard_url = None
    return s


def _http() -> AsyncMock:
    resp = MagicMock()
    resp.status_code = 404
    client = AsyncMock()
    client.get = AsyncMock(return_value=resp)
    return client


# ---------------------------------------------------------------------------
# thread creation timeout
# ---------------------------------------------------------------------------

async def test_thread_creation_timeout_returns_false(session, board):
    """TimeoutError from create_thread → handle_reaction returns False, no crash.

    This is the rate-limit path: the 15s asyncio.timeout fires during
    create_thread, we bail gracefully and let the bootup retry pick it up.
    """
    msg = _message(channel_id=board.discord_channel_id)
    msg.channel.create_thread.side_effect = TimeoutError

    result = await handle_reaction(
        session,
        settings=_settings(),
        message=msg,
        http_client=_http(),
        skip_auth=True,
    )

    assert result is False
    msg.channel.create_thread.assert_called_once()


# ---------------------------------------------------------------------------
# existing-thread path never calls create_thread
# ---------------------------------------------------------------------------

async def test_existing_thread_skips_create_thread(session, board):
    """When a SubmissionThread mapping already exists, create_thread is never
    called - so a thread-creation rate limit on a different submission does
    not affect this one at all.
    """
    msg = _message(channel_id=board.discord_channel_id)
    existing = _thread(thread_id=500)

    # Pre-populate: submission + thread mapping already in DB.
    sub = make_submission(board, source_discord_message_id=msg.id)
    sub.thread_id = 500
    session.add(sub)
    session.add(SubmissionThread(
        board_id=board.id,
        source_discord_message_id=msg.id,
        thread_id=500,
    ))
    await session.flush()

    # The existing thread is resolved from the guild's thread cache.
    msg.guild.get_thread.return_value = existing

    await handle_reaction(
        session,
        settings=_settings(),
        message=msg,
        http_client=_http(),
        skip_auth=True,
    )

    msg.channel.create_thread.assert_not_called()


# ---------------------------------------------------------------------------
# new thread success: recompute runs (sends into the thread)
# ---------------------------------------------------------------------------

async def test_new_thread_runs_recompute(session, board):
    """Successful thread creation → recompute_and_request runs and sends into
    the new thread (cancel button at minimum).
    """
    msg = _message(channel_id=board.discord_channel_id)
    new_thread = _thread(thread_id=600)
    msg.channel.create_thread.return_value = new_thread

    await handle_reaction(
        session,
        settings=_settings(),
        message=msg,
        http_client=_http(),
        skip_auth=True,
    )

    msg.channel.create_thread.assert_called_once()
    # recompute sends at least the cancel button
    assert new_thread.send.call_count >= 1


# ---------------------------------------------------------------------------
# Ingestion path: URL in message creates SubmissionLink
# ---------------------------------------------------------------------------

async def test_handle_reaction_ingests_url(session, board):
    """A message URL must flow all the way through to a SubmissionLink row.

    This guards the adapter boundary: if Discord's message.content is ever
    dropped when building InboundMessage, this test catches it.
    """
    msg = _message(channel_id=board.discord_channel_id)
    msg.content = "https://example.com/cool-robot-post"
    new_thread = _thread(thread_id=700)
    msg.channel.create_thread.return_value = new_thread

    with patch("bot.discord_ingest.service._resolve_links", new_callable=AsyncMock):
        await handle_reaction(
            session,
            settings=_settings(),
            message=msg,
            http_client=_http(),
            skip_auth=True,
        )

    links = list(await session.scalars(
        select(SubmissionLink).where(
            SubmissionLink.raw_url == "https://example.com/cool-robot-post"
        )
    ))
    assert len(links) == 1


async def test_handle_reaction_embed_url_ingested_when_no_text(session, board):
    """When message.content is empty, embed URL falls back to SubmissionLink.

    Guards the embed → InboundEmbed adapter boundary.
    """
    embed = MagicMock()
    embed.url = "https://example.com/from-embed"
    embed.title = None
    embed.description = None
    embed.thumbnail = None
    embed.image = None

    msg = _message(channel_id=board.discord_channel_id)
    msg.content = ""
    msg.embeds = [embed]
    new_thread = _thread(thread_id=701)
    msg.channel.create_thread.return_value = new_thread

    with patch("bot.discord_ingest.service._resolve_links", new_callable=AsyncMock):
        await handle_reaction(
            session,
            settings=_settings(),
            message=msg,
            http_client=_http(),
            skip_auth=True,
        )

    links = list(await session.scalars(
        select(SubmissionLink).where(
            SubmissionLink.raw_url == "https://example.com/from-embed"
        )
    ))
    assert len(links) == 1
