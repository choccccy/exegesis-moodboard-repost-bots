"""Tests for the YouTube playlist flows in the ingest service.

Covers _auto_add_to_playlist (queue-time auto-add of YouTube links),
_do_playlist_remove (removal + DB cleanup), handle_playlist_opt_out
(the stop-button reaction on the opt-out prompt), and _playlist_close_ready
(whether playlist state blocks thread archival).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from sqlalchemy import select

from bot.config import BoardConfig
from bot.state import SubmissionState
from bot.discord_ingest.service import (
    _auto_add_to_playlist,
    _do_playlist_remove,
    _playlist_close_ready,
    handle_playlist_opt_out,
)
from bot.models import SubmissionLink, YoutubePlaylistAdd

from conftest import MockDest, make_submission


def _board_cfg(board, *, playlist_id="PL_test123", curator_user_ids=None) -> BoardConfig:
    return BoardConfig(
        name=board.name,
        discord_guild_id=board.discord_guild_id,
        discord_channel_id=board.discord_channel_id,
        bluesky_handle=f"{board.name}.exegesis.space",
        tags=[],
        youtube_playlist_id=playlist_id,
        curator_user_ids=curator_user_ids or [],
    )


def _link(url: str, family: str = "youtube", order: int = 0) -> SubmissionLink:
    return SubmissionLink(
        submission_id=0,
        order_index=order,
        raw_url=url,
        canonical_url=url,
        domain_family=family,
    )


def _yt_client(item_id="item-abc"):
    client = MagicMock()
    client.add_to_playlist.return_value = item_id
    return client


async def _rows(session):
    return list(await session.scalars(select(YoutubePlaylistAdd)))


# ---------------------------------------------------------------------------
# _auto_add_to_playlist
# ---------------------------------------------------------------------------


async def test_auto_add_success_writes_row(session, board):
    sub = make_submission(board, source_discord_message_id=42)
    session.add(sub)
    await session.flush()
    yt = _yt_client("item-1")
    links = [_link("https://www.youtube.com/watch?v=abc123XYZ")]

    added = await _auto_add_to_playlist(session, sub, links, _board_cfg(board), yt)

    assert added == 1
    yt.add_to_playlist.assert_called_once_with("PL_test123", "abc123XYZ")
    rows = await _rows(session)
    assert len(rows) == 1
    row = rows[0]
    assert row.success is True
    assert row.playlist_item_id == "item-1"
    assert row.video_id == "abc123XYZ"
    assert row.playlist_id == "PL_test123"
    assert row.source_discord_message_id == 42
    assert row.discord_requester_id == sub.author_id
    assert row.error_message is None


async def test_auto_add_failure_writes_error_row(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    yt = _yt_client()
    yt.add_to_playlist.side_effect = RuntimeError("quota exceeded")
    links = [_link("https://youtu.be/def456UVW")]

    added = await _auto_add_to_playlist(session, sub, links, _board_cfg(board), yt)

    assert added == 0
    rows = await _rows(session)
    assert len(rows) == 1
    assert rows[0].success is False
    assert "quota exceeded" in rows[0].error_message
    assert rows[0].playlist_item_id is None
    assert rows[0].video_id == "def456UVW"


async def test_auto_add_no_yt_client_is_noop(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()

    added = await _auto_add_to_playlist(
        session, sub, [_link("https://youtu.be/abc")], _board_cfg(board), None
    )

    assert added == 0
    assert await _rows(session) == []


async def test_auto_add_board_without_playlist_id_is_noop(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    yt = _yt_client()

    added = await _auto_add_to_playlist(
        session, sub, [_link("https://youtu.be/abc")], _board_cfg(board, playlist_id=None), yt
    )

    assert added == 0
    yt.add_to_playlist.assert_not_called()
    assert await _rows(session) == []


async def test_auto_add_non_youtube_link_skipped(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    yt = _yt_client()

    added = await _auto_add_to_playlist(
        session, sub, [_link("https://example.com/video", family="other")], _board_cfg(board), yt
    )

    assert added == 0
    yt.add_to_playlist.assert_not_called()
    assert await _rows(session) == []


async def test_auto_add_dedupes_same_video_across_links(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    yt = _yt_client()
    links = [
        _link("https://www.youtube.com/watch?v=same1", order=0),
        _link("https://youtu.be/same1", order=1),
    ]

    added = await _auto_add_to_playlist(session, sub, links, _board_cfg(board), yt)

    assert added == 1
    assert yt.add_to_playlist.call_count == 1
    assert len(await _rows(session)) == 1


async def test_auto_add_skips_video_already_added_for_board(session, board):
    sub = make_submission(board, source_discord_message_id=50)
    session.add(sub)
    session.add(YoutubePlaylistAdd(
        board_id=board.id,
        source_discord_message_id=49,
        video_id="dupvid",
        playlist_id="PL_test123",
        discord_requester_id=1,
        success=True,
    ))
    await session.flush()
    yt = _yt_client()

    added = await _auto_add_to_playlist(
        session, sub, [_link("https://youtu.be/dupvid")], _board_cfg(board), yt
    )

    assert added == 0
    yt.add_to_playlist.assert_not_called()
    assert len(await _rows(session)) == 1  # only the pre-existing row


# ---------------------------------------------------------------------------
# _do_playlist_remove
# ---------------------------------------------------------------------------


def _playlist_row(board, *, item_id="item-9", video_id="vid9", requester=999):
    return YoutubePlaylistAdd(
        board_id=board.id,
        source_discord_message_id=42,
        video_id=video_id,
        playlist_id="PL_test123",
        discord_requester_id=requester,
        success=True,
        playlist_item_id=item_id,
    )


async def test_do_playlist_remove_success(session, board):
    row = _playlist_row(board)
    session.add(row)
    await session.flush()
    yt = _yt_client()
    dest = MockDest()

    await _do_playlist_remove(row, dest, session, yt)

    yt.remove_from_playlist.assert_called_once_with("item-9")
    assert await _rows(session) == []
    assert any("removed" in m and "vid9" in m for m in dest.sent)


async def test_do_playlist_remove_failure_keeps_row(session, board):
    row = _playlist_row(board)
    session.add(row)
    await session.flush()
    yt = _yt_client()
    yt.remove_from_playlist.side_effect = RuntimeError("api down")
    dest = MockDest()

    await _do_playlist_remove(row, dest, session, yt)

    assert any("failed to remove" in m for m in dest.sent)
    assert len(await _rows(session)) == 1  # row not deleted on failure


async def test_do_playlist_remove_without_item_id_skips_api(session, board):
    row = _playlist_row(board, item_id=None)
    session.add(row)
    await session.flush()
    yt = _yt_client()
    dest = MockDest()

    await _do_playlist_remove(row, dest, session, yt)

    yt.remove_from_playlist.assert_not_called()
    assert await _rows(session) == []
    assert any("removed" in m for m in dest.sent)


# ---------------------------------------------------------------------------
# handle_playlist_opt_out
# ---------------------------------------------------------------------------


def _opt_out_settings(board, cfg):
    settings = MagicMock()
    settings.board_for_channel = lambda cid: cfg if cid == board.discord_channel_id else None
    return settings


async def test_opt_out_unknown_prompt_message_ignored(session, board):
    settings = _opt_out_settings(board, _board_cfg(board))
    # No submission has playlist_opt_out_message_id == 12345; must return quietly.
    await handle_playlist_opt_out(
        session, message_id=12345, user_id=999, member=None,
        channel=MockDest(), settings=settings, yt_client=_yt_client(),
    )


async def test_opt_out_before_add_marks_skipped(session, board):
    sub = make_submission(board, playlist_opt_out_message_id=777)
    session.add(sub)
    await session.flush()
    yt = _yt_client()
    settings = _opt_out_settings(board, _board_cfg(board))

    await handle_playlist_opt_out(
        session, message_id=777, user_id=sub.author_id, member=None,
        channel=MockDest(), settings=settings, yt_client=yt,
    )

    assert sub.playlist_skipped is True
    yt.remove_from_playlist.assert_not_called()


async def test_opt_out_after_add_removes_from_playlist(session, board):
    sub = make_submission(board, source_discord_message_id=42, playlist_opt_out_message_id=778)
    session.add(sub)
    session.add(_playlist_row(board, item_id="item-77", video_id="vid77"))
    await session.flush()
    yt = _yt_client()
    dest = MockDest()
    settings = _opt_out_settings(board, _board_cfg(board))

    await handle_playlist_opt_out(
        session, message_id=778, user_id=sub.author_id, member=None,
        channel=dest, settings=settings, yt_client=yt,
    )

    assert sub.playlist_skipped is True
    yt.remove_from_playlist.assert_called_once_with("item-77")
    assert await _rows(session) == []
    assert any("removed" in m for m in dest.sent)


async def test_opt_out_unauthorized_user_ignored(session, board):
    sub = make_submission(board, source_discord_message_id=42, playlist_opt_out_message_id=779)
    session.add(sub)
    session.add(_playlist_row(board))
    await session.flush()
    yt = _yt_client()
    settings = _opt_out_settings(board, _board_cfg(board))

    await handle_playlist_opt_out(
        session, message_id=779, user_id=555, member=None,
        channel=MockDest(), settings=settings, yt_client=yt,
    )

    assert sub.playlist_skipped is False
    yt.remove_from_playlist.assert_not_called()
    assert len(await _rows(session)) == 1


async def test_opt_out_on_queued_submission_reschedules_archive(session, board):
    """Opting out of a QUEUED submission with an open thread schedules archival."""
    import discord

    sub = make_submission(
        board,
        state=SubmissionState.QUEUED.value,
        source_discord_message_id=42,
        playlist_opt_out_message_id=785,
        thread_id=9999,
    )
    session.add(sub)
    await session.flush()

    thread = MagicMock(spec=discord.Thread)
    thread.archived = False
    channel = MagicMock()
    channel.guild.get_thread = MagicMock(return_value=thread)
    settings = _opt_out_settings(board, _board_cfg(board))

    with patch("bot.discord_ingest.service._fire_and_forget") as mock_fnf:
        await handle_playlist_opt_out(
            session, message_id=785, user_id=sub.author_id, member=None,
            channel=channel, settings=settings, yt_client=_yt_client(),
        )

    assert sub.playlist_skipped is True
    mock_fnf.assert_called_once()
    mock_fnf.call_args.args[0].close()  # avoid a never-awaited coroutine warning


async def test_opt_out_explicit_curator_allowed(session, board):
    sub = make_submission(board, playlist_opt_out_message_id=780)
    session.add(sub)
    await session.flush()
    cfg = _board_cfg(board, curator_user_ids=[555])
    settings = _opt_out_settings(board, cfg)

    await handle_playlist_opt_out(
        session, message_id=780, user_id=555, member=None,
        channel=MockDest(), settings=settings, yt_client=_yt_client(),
    )

    assert sub.playlist_skipped is True


# ---------------------------------------------------------------------------
# _playlist_close_ready
# ---------------------------------------------------------------------------


async def test_close_ready_true_without_board_cfg(session, board):
    assert await _playlist_close_ready(session, board.id, 42, None) is True


async def test_close_ready_true_without_playlist_id(session, board):
    cfg = _board_cfg(board, playlist_id=None)
    assert await _playlist_close_ready(session, board.id, 42, cfg) is True


async def test_close_ready_true_when_skipped(session, board):
    cfg = _board_cfg(board)
    assert await _playlist_close_ready(session, board.id, 42, cfg, playlist_skipped=True) is True


async def test_close_ready_false_when_add_not_attempted(session, board):
    cfg = _board_cfg(board)
    assert await _playlist_close_ready(session, board.id, 42, cfg) is False


async def test_close_ready_true_once_row_exists_even_failed(session, board):
    cfg = _board_cfg(board)
    session.add(YoutubePlaylistAdd(
        board_id=board.id,
        source_discord_message_id=42,
        video_id="v",
        playlist_id="PL_test123",
        discord_requester_id=999,
        success=False,
        error_message="quota",
    ))
    await session.flush()
    assert await _playlist_close_ready(session, board.id, 42, cfg) is True
