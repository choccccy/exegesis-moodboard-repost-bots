"""Tests for the two cancel flows in service.py.

handle_cancel_reaction: an X reaction on the cancel-request message inside a
submission thread deletes the submission (OP or curator only), archives the
thread, and cleans up downloaded files.

handle_source_cancel_reaction: an X reaction on the original source post
cancels the pending submission and/or any playlist additions, cascading to
the YouTube client where a playlist item was created.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import discord
from sqlalchemy import select

from bot.curation.core import handle_cancel_reaction, handle_source_cancel_reaction
from bot.config import BoardConfig
from bot.curation.events import ReactionEvent
from bot.models import (
    CancellationRequest,
    Submission,
    SubmissionThread,
    YoutubePlaylistAdd,
)
from bot.state import SubmissionState

from conftest import MockDest, make_submission, make_test_settings


def _reaction_event(user_id: int, message_id: int, *, member=None, channel_id: int = 500):
    return ReactionEvent(user_id=user_id, message_id=message_id, channel_id=channel_id,
                         emoji="\N{CROSS MARK}", member=member)

OP_ID = 999          # make_submission default author_id
CURATOR_ID = 555
CURATOR_ROLE_ID = 42
RANDO_ID = 111

SOURCE_MSG_ID = 4200
CANCEL_MSG_ID = 777
THREAD_ID = 500


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _cfg(**kw) -> BoardConfig:
    defaults = dict(
        name="robots",
        discord_guild_id=1,
        discord_channel_id=100,
        curator_user_ids=[CURATOR_ID],
        curator_role_ids=[CURATOR_ROLE_ID],
    )
    defaults.update(kw)
    return BoardConfig(**defaults)


def _settings(cfg: BoardConfig | None = None):
    cfg = cfg or _cfg()
    s = make_test_settings()
    s.board_for_channel = lambda cid: cfg if cid == cfg.discord_channel_id else None
    return s


def _thread(thread_id: int = THREAD_ID) -> MagicMock:
    t = MagicMock(spec=discord.Thread)
    t.id = thread_id
    t.archived = False
    t.send = AsyncMock()
    t.edit = AsyncMock()
    return t


def _thread_channel(thread: MagicMock) -> MagicMock:
    """The channel the cancel reaction arrives in: the submission thread itself.

    Its guild resolves thread ids from the cache, returning our thread mock.
    """
    ch = MagicMock(spec=discord.Thread)
    ch.id = thread.id
    ch.guild = MagicMock()
    ch.guild.get_thread = MagicMock(return_value=thread)
    return ch


def _source_channel(channel_id: int = 100) -> MagicMock:
    """The board channel the source post lives in."""
    ch = MagicMock(spec=discord.TextChannel)
    ch.id = channel_id
    msg = MagicMock(spec=discord.Message)
    msg.clear_reaction = AsyncMock()
    ch.fetch_message = AsyncMock(return_value=msg)
    ch.guild = MagicMock()
    return ch


def _member_with_roles(*role_ids: int) -> MagicMock:
    m = MagicMock(spec=discord.Member)
    m.roles = [MagicMock(id=rid) for rid in role_ids]
    return m


async def _seed_submission(session, board, *, state=SubmissionState.INTENT_SUBMITTED.value,
                           thread_id=THREAD_ID, with_cancel_request=True):
    sub = make_submission(
        board, state=state,
        source_discord_message_id=SOURCE_MSG_ID, thread_id=thread_id,
    )
    session.add(sub)
    await session.flush()
    if with_cancel_request:
        session.add(CancellationRequest(submission_id=sub.id, bot_message_id=CANCEL_MSG_ID))
        await session.flush()
    return sub


def _playlist_row(board, *, requester=OP_ID, item_id="pl-item-1", video_id="vid123"):
    return YoutubePlaylistAdd(
        board_id=board.id,
        source_discord_message_id=SOURCE_MSG_ID,
        video_id=video_id,
        playlist_id="PL123",
        discord_requester_id=requester,
        success=True,
        playlist_item_id=item_id,
    )


# ---------------------------------------------------------------------------
# handle_cancel_reaction (X on the cancel-request message in the thread)
# ---------------------------------------------------------------------------

async def test_cancel_reaction_op_deletes_and_archives(session, board):
    """OP cancels: submission and its request rows are deleted, files cleaned
    up, thread archived, and the source trigger reaction is cleared."""
    sub = await _seed_submission(session, board)
    dest = MockDest()

    with patch("bot.curation.core.remove_submission_dir") as rm:
        await handle_cancel_reaction(
            session, _reaction_event(OP_ID, CANCEL_MSG_ID), dest, _settings(),
        )

    assert await session.get(Submission, sub.id) is None
    assert await session.scalar(select(CancellationRequest)) is None
    rm.assert_called_once()
    assert dest.archived  # thread archived through the port
    assert dest.sent  # reaction-removed notice
    assert dest.cleared_triggers == [(board.discord_channel_id, SOURCE_MSG_ID)]


async def test_cancel_reaction_explicit_curator_authorized(session, board):
    """A user in curator_user_ids may cancel someone else's submission."""
    sub = await _seed_submission(session, board)
    dest = MockDest()

    with patch("bot.curation.core.remove_submission_dir"):
        await handle_cancel_reaction(
            session, _reaction_event(CURATOR_ID, CANCEL_MSG_ID), dest, _settings(),
        )

    assert dest.cleared_triggers == [(board.discord_channel_id, SOURCE_MSG_ID)]
    assert await session.get(Submission, sub.id) is None


async def test_cancel_reaction_role_curator_authorized(session, board):
    """A member holding a curator role may cancel."""
    sub = await _seed_submission(session, board)
    member = _member_with_roles(CURATOR_ROLE_ID)
    dest = MockDest()

    with patch("bot.curation.core.remove_submission_dir"):
        await handle_cancel_reaction(
            session, _reaction_event(RANDO_ID, CANCEL_MSG_ID, member=member), dest, _settings(),
        )

    assert dest.cleared_triggers == [(board.discord_channel_id, SOURCE_MSG_ID)]
    assert await session.get(Submission, sub.id) is None


async def test_cancel_reaction_non_curator_ignored(session, board):
    """Neither OP nor curator: nothing is deleted and no cleanup runs."""
    sub = await _seed_submission(session, board)
    member = _member_with_roles(7)  # unrelated role
    dest = MockDest()

    with patch("bot.curation.core.remove_submission_dir") as rm:
        await handle_cancel_reaction(
            session, _reaction_event(RANDO_ID, CANCEL_MSG_ID, member=member), dest, _settings(),
        )

    assert await session.get(Submission, sub.id) is not None
    rm.assert_not_called()
    assert not dest.archived


async def test_cancel_reaction_unknown_message_ignored(session, board):
    """A reaction on a message with no CancellationRequest row is a no-op."""
    await _seed_submission(session, board)
    dest = MockDest()

    with patch("bot.curation.core.remove_submission_dir") as rm:
        await handle_cancel_reaction(
            session, _reaction_event(OP_ID, 123456), dest, _settings(),
        )

    assert not dest.cleared_triggers
    rm.assert_not_called()


async def test_cancel_reaction_orphaned_request_ignored(session, board):
    """A CancellationRequest whose submission is gone is a no-op."""
    session.add(CancellationRequest(submission_id=98765, bot_message_id=CANCEL_MSG_ID))
    await session.flush()
    dest = MockDest()

    await handle_cancel_reaction(
        session, _reaction_event(OP_ID, CANCEL_MSG_ID), dest, _settings(),
    )

    assert not dest.cleared_triggers


async def test_cancel_reaction_published_is_terminal(session, board):
    """A published submission cannot be cancelled: the bot explains in the
    thread, keeps the row, and does not archive."""
    sub = await _seed_submission(session, board, state=SubmissionState.PUBLISHED.value)
    dest = MockDest()

    with patch("bot.curation.core.remove_submission_dir") as rm:
        await handle_cancel_reaction(
            session, _reaction_event(OP_ID, CANCEL_MSG_ID), dest, _settings(),
        )

    assert await session.get(Submission, sub.id) is not None
    rm.assert_not_called()
    assert len(dest.sent) == 1  # cannot-remove-published notice
    assert not dest.archived
    assert not dest.cleared_triggers


# ---------------------------------------------------------------------------
# handle_source_cancel_reaction (X on the original source post)
# ---------------------------------------------------------------------------

def _src_event(user_id: int, *, member=None):
    """A ❌ reaction on the source post (board channel 100)."""
    return _reaction_event(user_id, SOURCE_MSG_ID, member=member, channel_id=100)


async def test_source_cancel_op_cancels_submission(session, board):
    """OP X on the source post deletes the submission, clears the trigger
    reaction through the port, and notifies + archives the thread surface."""
    sub = await _seed_submission(session, board, with_cancel_request=False)
    dest = MockDest()

    with patch("bot.curation.core.remove_submission_dir") as rm:
        await handle_source_cancel_reaction(session, _src_event(OP_ID), dest, _settings())

    assert await session.get(Submission, sub.id) is None
    rm.assert_called_once()
    assert dest.cleared_triggers == [(100, SOURCE_MSG_ID)]
    assert dest.sent  # source-cancel confirmation
    assert dest.archived


async def test_source_cancel_explicit_curator_cancels(session, board):
    sub = await _seed_submission(session, board, with_cancel_request=False)
    dest = MockDest()

    with patch("bot.curation.core.remove_submission_dir"):
        await handle_source_cancel_reaction(session, _src_event(CURATOR_ID), dest, _settings())

    assert dest.cleared_triggers == [(100, SOURCE_MSG_ID)]
    assert await session.get(Submission, sub.id) is None


async def test_source_cancel_role_curator_cancels(session, board):
    sub = await _seed_submission(session, board, with_cancel_request=False)
    member = _member_with_roles(CURATOR_ROLE_ID)
    dest = MockDest()

    with patch("bot.curation.core.remove_submission_dir"):
        await handle_source_cancel_reaction(
            session, _src_event(RANDO_ID, member=member), dest, _settings(),
        )

    assert dest.cleared_triggers == [(100, SOURCE_MSG_ID)]
    assert await session.get(Submission, sub.id) is None


async def test_source_cancel_unauthorized_ignored(session, board):
    """A random user's X leaves the submission alone."""
    sub = await _seed_submission(session, board, with_cancel_request=False)
    dest = MockDest()

    with patch("bot.curation.core.remove_submission_dir") as rm:
        await handle_source_cancel_reaction(
            session, _src_event(RANDO_ID, member=_member_with_roles(7)), dest, _settings(),
        )

    assert await session.get(Submission, sub.id) is not None
    rm.assert_not_called()
    assert not dest.cleared_triggers


async def test_source_cancel_unknown_channel_ignored(session, board):
    """A channel with no Board row short-circuits (no work, no notices)."""
    dest = MockDest()
    await handle_source_cancel_reaction(
        session, _reaction_event(OP_ID, SOURCE_MSG_ID, channel_id=888), dest, _settings(),
    )
    assert not dest.sent
    assert not dest.cleared_triggers


async def test_source_cancel_no_submission_for_message(session, board):
    """Board exists but nothing was submitted for this message: no-op."""
    dest = MockDest()
    await handle_source_cancel_reaction(session, _src_event(OP_ID), dest, _settings())
    assert not dest.sent
    assert not dest.cleared_triggers


async def test_source_cancel_published_submission_kept(session, board):
    """Published submissions survive a source-post X."""
    sub = await _seed_submission(
        session, board, state=SubmissionState.PUBLISHED.value, with_cancel_request=False,
    )
    dest = MockDest()

    with patch("bot.curation.core.remove_submission_dir") as rm:
        await handle_source_cancel_reaction(session, _src_event(OP_ID), dest, _settings())

    assert await session.get(Submission, sub.id) is not None
    rm.assert_not_called()
    assert not dest.cleared_triggers


async def test_source_cancel_playlist_cascade_removes_item(session, board):
    """A successful playlist add is undone via yt_client, its row deleted, and a
    removal notice posted through the surface."""
    session.add(_playlist_row(board))
    await session.flush()
    yt = MagicMock()
    dest = MockDest()

    await handle_source_cancel_reaction(session, _src_event(OP_ID), dest, _settings(), yt_client=yt)

    yt.remove_from_playlist.assert_called_once_with("pl-item-1")
    assert await session.scalar(select(YoutubePlaylistAdd)) is None
    assert any("vid123" in m for m in dest.sent)


async def test_source_cancel_playlist_removed_even_without_yt_client(session, board):
    """No yt_client: the audit row still goes away and the video id is reported."""
    session.add(_playlist_row(board))
    await session.flush()
    dest = MockDest()

    await handle_source_cancel_reaction(session, _src_event(OP_ID), dest, _settings(), yt_client=None)

    assert await session.scalar(select(YoutubePlaylistAdd)) is None
    assert any("vid123" in m for m in dest.sent)


async def test_source_cancel_playlist_api_error_still_deletes_row(session, board):
    """yt_client failure is logged and swallowed; the row is deleted anyway."""
    session.add(_playlist_row(board))
    await session.flush()
    yt = MagicMock()
    yt.remove_from_playlist.side_effect = RuntimeError("quota")
    dest = MockDest()

    await handle_source_cancel_reaction(session, _src_event(OP_ID), dest, _settings(), yt_client=yt)

    assert await session.scalar(select(YoutubePlaylistAdd)) is None


async def test_source_cancel_playlist_requires_requester_or_curator(session, board):
    """A user who neither requested the add nor curates cannot undo it."""
    session.add(_playlist_row(board, requester=31337))
    await session.flush()
    yt = MagicMock()
    dest = MockDest()

    await handle_source_cancel_reaction(
        session, _src_event(RANDO_ID), dest, _settings(), yt_client=yt,
    )

    yt.remove_from_playlist.assert_not_called()
    assert await session.scalar(select(YoutubePlaylistAdd)) is not None
    assert not dest.sent
