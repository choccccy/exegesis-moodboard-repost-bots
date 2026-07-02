"""Tests for pure / lightly-mocked ingestion helpers in service.py.

Covers _derive_thread_title, _is_curator, and _post_thread_anchor so that after
the platform-agnostic refactor we can confirm behaviour is unchanged.
"""

from __future__ import annotations

import discord
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from bot.config import BoardConfig
from bot.discord_ingest.service import (
    _derive_thread_title,
    _is_curator,
    _post_thread_anchor,
)
from bot.ingest.types import InboundEmbed, InboundMessage
from bot.models import Submission, SubmissionLink
from bot.state import SubmissionState, GraphicStatus

from conftest import make_submission


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _board_cfg(**kw) -> BoardConfig:
    defaults = dict(
        name="robots",
        discord_guild_id=1,
        discord_channel_id=100,
        curator_role_ids=[],
        curator_user_ids=[],
    )
    defaults.update(kw)
    return BoardConfig(**defaults)


def _member(role_ids: list[int] = []) -> MagicMock:
    m = MagicMock(spec=discord.Member)
    m.roles = [MagicMock(id=rid) for rid in role_ids]
    return m


# ---------------------------------------------------------------------------
# _is_curator
# ---------------------------------------------------------------------------

def test_is_curator_explicit_user_id():
    cfg = _board_cfg(curator_user_ids=[42])
    assert _is_curator(None, 42, cfg) is True


def test_is_curator_explicit_user_id_no_member_needed():
    # member=None should not matter if user_id is in the list
    cfg = _board_cfg(curator_user_ids=[42])
    assert _is_curator(None, 42, cfg) is True


def test_is_curator_role_match():
    cfg = _board_cfg(curator_role_ids=[999])
    member = _member(role_ids=[999])
    assert _is_curator(member, 1, cfg) is True


def test_is_curator_role_no_match():
    cfg = _board_cfg(curator_role_ids=[999])
    member = _member(role_ids=[888])
    assert _is_curator(member, 1, cfg) is False


def test_is_curator_none_board_cfg():
    assert _is_curator(_member(), 42, None) is False


def test_is_curator_none_member_no_user_id():
    cfg = _board_cfg(curator_user_ids=[42])
    assert _is_curator(None, 99, cfg) is False


def test_is_curator_not_in_user_ids_not_in_roles():
    cfg = _board_cfg(curator_role_ids=[999], curator_user_ids=[42])
    member = _member(role_ids=[888])
    assert _is_curator(member, 1, cfg) is False


# ---------------------------------------------------------------------------
# _derive_thread_title
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_derive_title_uses_resolved_title(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value)
    session.add(sub)
    await session.flush()
    link = SubmissionLink(
        submission_id=sub.id,
        order_index=0,
        raw_url="https://example.com",
        canonical_url="https://example.com",
        domain_family="other",
        resolved_title="My Resolved Title",
    )
    session.add(link)
    await session.flush()

    message = InboundMessage()
    result = await _derive_thread_title(session, message, sub)
    assert result == "My Resolved Title"


@pytest.mark.asyncio
async def test_derive_title_embed_fallback(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value)
    session.add(sub)
    await session.flush()
    # no SubmissionLink → no resolved_title

    message = InboundMessage(embeds=[InboundEmbed(title="Embed Title")])
    result = await _derive_thread_title(session, message, sub)
    assert result == "Embed Title"


@pytest.mark.asyncio
async def test_derive_title_embed_author_fallback(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value)
    session.add(sub)
    await session.flush()

    message = InboundMessage(embeds=[InboundEmbed(author_name="Author Name")])
    result = await _derive_thread_title(session, message, sub)
    assert result == "Author Name"


@pytest.mark.asyncio
async def test_derive_title_generic_fallback(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value)
    session.add(sub)
    await session.flush()

    message = InboundMessage()
    result = await _derive_thread_title(session, message, sub)
    assert str(sub.id) in result
    assert "🦋" in result


@pytest.mark.asyncio
async def test_derive_title_truncates_long_title(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value)
    session.add(sub)
    await session.flush()
    link = SubmissionLink(
        submission_id=sub.id,
        order_index=0,
        raw_url="https://example.com",
        canonical_url="https://example.com",
        domain_family="other",
        resolved_title="A" * 101,
    )
    session.add(link)
    await session.flush()

    message = InboundMessage()
    result = await _derive_thread_title(session, message, sub)
    assert len(result) <= 100
    assert result.endswith("…")


@pytest.mark.asyncio
async def test_derive_title_strips_whitespace(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value)
    session.add(sub)
    await session.flush()
    link = SubmissionLink(
        submission_id=sub.id,
        order_index=0,
        raw_url="https://example.com",
        canonical_url="https://example.com",
        domain_family="other",
        resolved_title="  Padded Title  ",
    )
    session.add(link)
    await session.flush()

    message = InboundMessage()
    result = await _derive_thread_title(session, message, sub)
    assert result == "Padded Title"


# ---------------------------------------------------------------------------
# _post_thread_anchor
# ---------------------------------------------------------------------------

def _settings_mock(channel_id: int = 100, board_cfg: BoardConfig | None = None) -> MagicMock:
    s = MagicMock()
    s.board_for_channel.return_value = board_cfg
    s.dashboard_url = "http://dash.example.com"
    return s


@pytest.mark.asyncio
async def test_post_thread_anchor_with_content_title(session, board):
    """content_title is forwarded into the anchor text (the 📌 line)."""
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value, author_id=777)
    session.add(sub)
    await session.flush()

    thread = MagicMock(spec=discord.Thread)
    thread.send = AsyncMock(return_value=MagicMock(id=1))
    message = MagicMock(spec=discord.Message)
    message.forward = AsyncMock()
    message.guild = MagicMock()
    message.guild.id = 1

    await _post_thread_anchor(_settings_mock(), message, sub, thread, content_title="My Robot Photo")
    anchor_text = thread.send.call_args_list[0].args[0]
    assert "My Robot Photo" in anchor_text


@pytest.mark.asyncio
async def test_post_thread_anchor_sends_message(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value, author_id=777)
    session.add(sub)
    await session.flush()

    thread = MagicMock(spec=discord.Thread)
    thread.send = AsyncMock(return_value=MagicMock(id=1))
    message = MagicMock(spec=discord.Message)
    message.forward = AsyncMock()
    message.guild = MagicMock()
    message.guild.id = 1

    await _post_thread_anchor(_settings_mock(), message, sub, thread)
    thread.send.assert_awaited()
    first_call_text = thread.send.call_args_list[0].args[0]
    assert first_call_text  # non-empty anchor text


@pytest.mark.asyncio
async def test_post_thread_anchor_forwards_source(session, board):
    sub = make_submission(board, state=SubmissionState.INTENT_SUBMITTED.value, author_id=777)
    session.add(sub)
    await session.flush()

    thread = MagicMock(spec=discord.Thread)
    thread.send = AsyncMock(return_value=MagicMock(id=1))
    message = MagicMock(spec=discord.Message)
    message.forward = AsyncMock()
    message.guild = MagicMock()
    message.guild.id = 1

    await _post_thread_anchor(_settings_mock(), message, sub, thread)
    message.forward.assert_awaited_once_with(thread)


@pytest.mark.asyncio
async def test_post_thread_anchor_forward_fallback(session, board):
    """When forward() fails, a jump link is sent instead."""
    sub = make_submission(
        board,
        state=SubmissionState.INTENT_SUBMITTED.value,
        author_id=777,
        source_discord_message_id=42,
    )
    session.add(sub)
    await session.flush()

    thread = MagicMock(spec=discord.Thread)
    thread.send = AsyncMock(return_value=MagicMock(id=1))
    message = MagicMock(spec=discord.Message)
    message.forward = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "fail"))
    message.guild = MagicMock()
    message.guild.id = 1

    await _post_thread_anchor(_settings_mock(), message, sub, thread)
    # second send call should be the jump link
    assert thread.send.await_count >= 2
    jump_call_text = thread.send.call_args_list[-1].args[0]
    assert "discord.com/channels" in jump_call_text
