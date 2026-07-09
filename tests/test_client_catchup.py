"""Tests for RepostBot catch-up scans, archival loops, and channel helpers.

Covers _run_channel_scan (trigger detection, skip, error guard, scan
bookkeeping), _run_catchup (per-channel loop, unresolvable channel, history
failure guard), _run_thread_catchup (reaction/reply replay, missing thread,
history failure), _archive_queued_threads scheduling, _archive_thread_by_id
branches, _run_threadless_retry_loop, and the _resolve_channel /
_fetch_message / _is_watched_location helpers.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import discord
import pytest

from bot.discord_ingest.client import RepostBot
from bot.models import SubmissionThread
from bot.state import SubmissionState
from conftest import bound_session_scope, make_submission


def _patch_user(uid: int = 42):
    return patch.object(
        RepostBot, "user", new_callable=PropertyMock, return_value=SimpleNamespace(id=uid)
    )


def _not_found() -> discord.NotFound:
    return discord.NotFound(MagicMock(status=404, reason="Not Found"), "gone")


def install_history(channel, messages, *, error=None):
    """Make channel.history(...) return an async iterator over `messages`.

    If `error` is given it is raised after the messages are exhausted,
    simulating a mid-iteration API failure.
    """

    def _history(*args, **kwargs):
        async def gen():
            for m in messages:
                yield m
            if error is not None:
                raise error

        return gen()

    channel.history = MagicMock(side_effect=_history)


def make_history_message(*, msg_id=555, author_id=7, emojis=(), reference=None):
    message = MagicMock()
    message.id = msg_id
    message.author.id = author_id
    message.reference = reference
    reaction_mocks = []
    for emoji in emojis:
        r = MagicMock()
        r.emoji = emoji
        reaction_mocks.append(r)
    message.reactions = reaction_mocks
    return message


def make_reaction(emoji, user_ids):
    """Reaction mock whose .users() returns a fresh async iterator per call."""
    r = MagicMock()
    r.emoji = emoji

    def _users():
        async def gen():
            for uid in user_ids:
                yield SimpleNamespace(id=uid)

        return gen()

    r.users = MagicMock(side_effect=_users)
    return r


# ---------------------------------------------------------------------------
# _run_channel_scan
# ---------------------------------------------------------------------------


async def test_channel_scan_processes_trigger_messages(repost_bot, session):
    channel = MagicMock()
    channel.id = 100
    channel.name = "robots"
    install_history(channel, [
        make_history_message(msg_id=1, emojis=["\N{BUTTERFLY}"]),
        make_history_message(msg_id=2, emojis=["\N{THUMBS UP SIGN}"]),
        make_history_message(msg_id=3, emojis=[]),
    ])
    status = MagicMock()
    status.edit = AsyncMock()
    repost_bot._http = MagicMock()
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch("bot.discord_ingest.client.asyncio.sleep", new_callable=AsyncMock),
        patch("bot.discord_ingest.client.scan_started") as started,
        patch("bot.discord_ingest.client.scan_finished") as finished,
        patch(
            "bot.discord_ingest.client.service.handle_reaction",
            new_callable=AsyncMock, return_value=True,
        ) as handler,
    ):
        await repost_bot._run_channel_scan(channel, cutoff, status)
    handler.assert_awaited_once()  # only the butterflied message
    started.assert_called_once_with(100, "robots", scan_type="manual")
    finished.assert_called_once_with(100)
    summary = status.edit.await_args.kwargs["content"]
    assert "3 messages checked" in summary
    assert "1 new submission(s)" in summary


async def test_channel_scan_reports_progress_every_500_messages(repost_bot, session):
    channel = MagicMock()
    channel.id = 100
    install_history(channel, [make_history_message(msg_id=i, emojis=[]) for i in range(500)])
    status = MagicMock()
    # The progress edit failing (Forbidden) must not abort the scan.
    status.edit = AsyncMock(side_effect=[
        discord.Forbidden(MagicMock(status=403, reason="Forbidden"), "no"),
        None,
    ])
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    with (
        patch("bot.discord_ingest.client.scan_started"),
        patch("bot.discord_ingest.client.scan_finished"),
    ):
        await repost_bot._run_channel_scan(channel, cutoff, status)
    # One progress edit at message 500, then the final summary edit.
    assert status.edit.await_count == 2
    progress = status.edit.await_args_list[0].kwargs["content"]
    assert "500 messages checked" in progress


async def test_channel_scan_timeout_reports_and_tolerates_edit_failure(repost_bot):
    # A TimeoutError escaping the history walk means the per-channel wall clock
    # expired; the summary says so, and a failing status edit is swallowed.
    channel = MagicMock()
    channel.id = 100
    install_history(channel, [], error=TimeoutError())
    status = MagicMock()
    status.edit = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403, reason="Forbidden"), "no"))
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    with (
        patch("bot.discord_ingest.client.scan_started"),
        patch("bot.discord_ingest.client.scan_finished") as finished,
    ):
        await repost_bot._run_channel_scan(channel, cutoff, status)  # must not raise
    finished.assert_called_once_with(100)
    assert "timed out" in status.edit.await_args.kwargs["content"]


async def test_channel_scan_survives_handler_errors(repost_bot, session):
    channel = MagicMock()
    channel.id = 100
    install_history(channel, [make_history_message(emojis=["\N{BUTTERFLY}"])])
    repost_bot._http = MagicMock()
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch("bot.discord_ingest.client.asyncio.sleep", new_callable=AsyncMock),
        patch("bot.discord_ingest.client.scan_started"),
        patch("bot.discord_ingest.client.scan_finished") as finished,
        patch(
            "bot.discord_ingest.client.service.handle_reaction",
            new_callable=AsyncMock, side_effect=RuntimeError("boom"),
        ),
    ):
        await repost_bot._run_channel_scan(channel, cutoff, None)  # must not raise
    finished.assert_called_once_with(100)


# ---------------------------------------------------------------------------
# _run_catchup
# ---------------------------------------------------------------------------


async def test_run_catchup_scans_watched_channels(repost_bot, session):
    channel = MagicMock()
    channel.name = "robots"
    install_history(channel, [
        make_history_message(msg_id=1, emojis=["\N{BUTTERFLY}"]),
        make_history_message(msg_id=2, emojis=[]),
    ])
    repost_bot._resolve_channel = AsyncMock(return_value=channel)
    repost_bot._run_thread_catchup = AsyncMock()
    repost_bot._http = MagicMock()
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch("bot.discord_ingest.client.asyncio.sleep", new_callable=AsyncMock),
        patch("bot.discord_ingest.client.scan_started") as started,
        patch("bot.discord_ingest.client.scan_finished") as finished,
        patch(
            "bot.discord_ingest.client.service.handle_reaction",
            new_callable=AsyncMock, return_value=False,
        ) as handler,
    ):
        await repost_bot._run_catchup()
    # Let the follow-up thread-catchup task run (outside the block, where
    # asyncio.sleep is no longer patched away).
    import asyncio
    await asyncio.sleep(0)
    handler.assert_awaited_once()
    started.assert_called_once_with(100, "robots", scan_type="catchup")
    finished.assert_called_once_with(100)
    repost_bot._run_thread_catchup.assert_awaited_once()


async def test_run_catchup_skips_unresolvable_channel(repost_bot):
    repost_bot._resolve_channel = AsyncMock(return_value=None)
    repost_bot._run_thread_catchup = AsyncMock()
    with (
        patch("bot.discord_ingest.client.scan_started") as started,
        patch(
            "bot.discord_ingest.client.service.handle_reaction",
            new_callable=AsyncMock,
        ) as handler,
    ):
        await repost_bot._run_catchup()
        import asyncio
        await asyncio.sleep(0)
    started.assert_not_called()
    handler.assert_not_awaited()
    repost_bot._run_thread_catchup.assert_awaited_once()


async def test_run_catchup_survives_history_failure(repost_bot):
    channel = MagicMock()
    channel.name = "robots"
    install_history(channel, [], error=discord.Forbidden(MagicMock(status=403, reason="Forbidden"), "no"))
    repost_bot._resolve_channel = AsyncMock(return_value=channel)
    repost_bot._run_thread_catchup = AsyncMock()
    with (
        patch("bot.discord_ingest.client.scan_started"),
        patch("bot.discord_ingest.client.scan_finished") as finished,
    ):
        await repost_bot._run_catchup()  # must not raise
        import asyncio
        await asyncio.sleep(0)
    finished.assert_called_once_with(100)


async def test_run_catchup_handler_error_then_timeout(repost_bot, session):
    # A per-message failure is logged and the walk continues; a wall-clock
    # timeout ends the channel scan without aborting the whole catch-up.
    channel = MagicMock()
    channel.name = "robots"
    install_history(
        channel,
        [make_history_message(msg_id=1, emojis=["\N{BUTTERFLY}"])],
        error=TimeoutError(),
    )
    repost_bot._resolve_channel = AsyncMock(return_value=channel)
    repost_bot._run_thread_catchup = AsyncMock()
    repost_bot._http = MagicMock()
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch("bot.discord_ingest.client.asyncio.sleep", new_callable=AsyncMock),
        patch("bot.discord_ingest.client.scan_started"),
        patch("bot.discord_ingest.client.scan_finished") as finished,
        patch(
            "bot.discord_ingest.client.service.handle_reaction",
            new_callable=AsyncMock, side_effect=RuntimeError("boom"),
        ),
    ):
        await repost_bot._run_catchup()  # must not raise
    import asyncio
    await asyncio.sleep(0)
    finished.assert_called_once_with(100)
    repost_bot._run_thread_catchup.assert_awaited_once()


# ---------------------------------------------------------------------------
# _run_thread_catchup
# ---------------------------------------------------------------------------


async def _add_pending_thread(session, board, *, source_msg_id=1, thread_id=777,
                              state=SubmissionState.AWAITING_ALT_TEXT.value,
                              updated_at=None):
    sub = make_submission(board, state=state, source_discord_message_id=source_msg_id)
    if updated_at is not None:
        sub.updated_at = updated_at
    session.add(sub)
    session.add(SubmissionThread(
        board_id=board.id, source_discord_message_id=source_msg_id, thread_id=thread_id,
    ))
    await session.flush()
    return sub


async def test_thread_catchup_replays_reactions_and_replies(repost_bot, session, board):
    sub = await _add_pending_thread(session, board)
    thread = MagicMock()
    bot_msg = make_history_message(msg_id=900, author_id=42)
    bot_msg.reactions = [
        # The bot's own id (42) must be skipped in every replay loop.
        make_reaction("\N{DROP OF BLOOD}", [42, 7]),
        make_reaction("\N{LINK SYMBOL}", [42, 7]),
        make_reaction("\N{CROSS MARK}", [42, 7]),
        make_reaction("\N{THUMBS UP SIGN}", [7]),  # not replayed
    ]
    human_reply = make_history_message(msg_id=901, author_id=7, reference=MagicMock())
    human_chatter = make_history_message(msg_id=902, author_id=7, reference=None)
    install_history(thread, [bot_msg, human_reply, human_chatter])
    repost_bot._resolve_channel = AsyncMock(return_value=thread)
    repost_bot._http = MagicMock()
    fired = []
    with (
        _patch_user(42),
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch("bot.discord_ingest.client.asyncio.sleep", new_callable=AsyncMock),
        patch("bot.discord_ingest.client.service.handle_label_reaction", new_callable=AsyncMock) as label,
        patch("bot.discord_ingest.client.service.handle_metadata_reaction", new_callable=AsyncMock) as meta,
        patch("bot.discord_ingest.client.service.handle_cancel_reaction", new_callable=AsyncMock) as cancel,
        patch("bot.discord_ingest.client.service.handle_reply", new_callable=AsyncMock) as reply,
        patch("bot.discord_ingest.client.service.recompute_and_request", new_callable=AsyncMock) as recompute,
        patch(
            "bot.discord_ingest.client.service._fire_and_forget",
            side_effect=lambda coro: (fired.append(coro), coro.close()),
        ),
    ):
        await repost_bot._run_thread_catchup()
    label.assert_awaited_once()
    assert label.await_args.kwargs["user_id"] == 7  # bot's own vote skipped
    meta.assert_awaited_once()
    assert meta.await_args.kwargs["user_id"] == 7
    cancel.assert_awaited_once()
    assert cancel.await_args.kwargs["user_id"] == 7
    reply.assert_awaited_once()
    assert reply.await_args.kwargs["message"] is human_reply
    recompute.assert_awaited_once()
    assert recompute.await_args.args[1].id == sub.id
    assert len(fired) == 1  # queued-thread archival scan scheduled at the end


async def test_thread_catchup_skips_missing_thread(repost_bot, session, board):
    await _add_pending_thread(session, board)
    repost_bot._resolve_channel = AsyncMock(return_value=None)
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch("bot.discord_ingest.client.asyncio.sleep", new_callable=AsyncMock),
        patch("bot.discord_ingest.client.service.recompute_and_request", new_callable=AsyncMock) as recompute,
        patch("bot.discord_ingest.client.service._fire_and_forget", side_effect=lambda coro: coro.close()),
    ):
        await repost_bot._run_thread_catchup()
    recompute.assert_not_awaited()


async def test_thread_catchup_recompute_failure_is_logged_not_raised(repost_bot, session, board):
    await _add_pending_thread(session, board)
    thread = MagicMock()
    install_history(thread, [])
    repost_bot._resolve_channel = AsyncMock(return_value=thread)
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch("bot.discord_ingest.client.asyncio.sleep", new_callable=AsyncMock),
        patch(
            "bot.discord_ingest.client.service.recompute_and_request",
            new_callable=AsyncMock, side_effect=RuntimeError("boom"),
        ) as recompute,
        patch("bot.discord_ingest.client.service._fire_and_forget", side_effect=lambda coro: coro.close()),
    ):
        await repost_bot._run_thread_catchup()  # must not raise
    recompute.assert_awaited_once()


async def test_thread_catchup_submission_gone_skips_recompute(repost_bot, session, board):
    await _add_pending_thread(session, board)
    thread = MagicMock()
    install_history(thread, [])
    repost_bot._resolve_channel = AsyncMock(return_value=thread)
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch("bot.discord_ingest.client.asyncio.sleep", new_callable=AsyncMock),
        patch.object(session, "get", new=AsyncMock(return_value=None)),
        patch("bot.discord_ingest.client.service.recompute_and_request", new_callable=AsyncMock) as recompute,
        patch("bot.discord_ingest.client.service._fire_and_forget", side_effect=lambda coro: coro.close()),
    ):
        await repost_bot._run_thread_catchup()
    recompute.assert_not_awaited()


async def test_thread_catchup_survives_history_failure(repost_bot, session, board):
    await _add_pending_thread(session, board)
    thread = MagicMock()
    install_history(thread, [], error=discord.Forbidden(MagicMock(status=403, reason="Forbidden"), "no"))
    repost_bot._resolve_channel = AsyncMock(return_value=thread)
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch("bot.discord_ingest.client.asyncio.sleep", new_callable=AsyncMock),
        patch("bot.discord_ingest.client.service.recompute_and_request", new_callable=AsyncMock) as recompute,
        patch("bot.discord_ingest.client.service._fire_and_forget", side_effect=lambda coro: coro.close()),
    ):
        await repost_bot._run_thread_catchup()  # must not raise
    recompute.assert_not_awaited()  # `continue` skips the recompute step


def _checklist_thread():
    """A thread mock that records sends and supports edit-in-place (get_partial_message)."""
    thread = MagicMock()
    install_history(thread, [])
    thread.sent = []

    async def _send(content=None, **kw):
        thread.sent.append(content or "")
        m = MagicMock()
        m.id = 55555
        return m

    thread.send = _send

    def _partial(message_id):
        p = MagicMock()
        p.edit = AsyncMock()
        return p

    thread.get_partial_message = _partial
    return thread


async def test_thread_catchup_backfills_checklist_on_idle_pending(repost_bot, session, board):
    # An idle pending submission (no source, nothing to replay) is the backlog case.
    sub = await _add_pending_thread(session, board)
    thread = _checklist_thread()
    repost_bot._resolve_channel = AsyncMock(return_value=thread)
    repost_bot._http = MagicMock()
    with (
        _patch_user(42),
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch("bot.discord_ingest.client.asyncio.sleep", new_callable=AsyncMock) as sleep,
        patch("bot.discord_ingest.client.service._fire_and_forget", side_effect=lambda coro: coro.close()),
    ):
        await repost_bot._run_thread_catchup()

    # The real recompute ran and posted a status checklist to the idle thread...
    checklists = [c for c in thread.sent if c.startswith("**post status**")]
    assert len(checklists) == 1
    # ...its id was persisted so future passes edit in place...
    await session.refresh(sub)
    assert sub.status_message_id == 55555
    # ...and the pass was paced.
    sleep.assert_awaited()


async def test_thread_catchup_does_not_duplicate_existing_checklist(repost_bot, session, board):
    sub = await _add_pending_thread(session, board)
    sub.status_message_id = 55555  # checklist already exists from a prior run
    await session.flush()
    thread = _checklist_thread()
    repost_bot._resolve_channel = AsyncMock(return_value=thread)
    repost_bot._http = MagicMock()
    with (
        _patch_user(42),
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch("bot.discord_ingest.client.asyncio.sleep", new_callable=AsyncMock),
        patch("bot.discord_ingest.client.service._fire_and_forget", side_effect=lambda coro: coro.close()),
    ):
        await repost_bot._run_thread_catchup()

    # No new checklist message: it was edited in place instead of re-sent.
    assert not any(c.startswith("**post status**") for c in thread.sent)


async def test_thread_catchup_includes_ready_to_queue_submissions(repost_bot, session, board):
    """READY_TO_QUEUE submissions must be included in the catch-up scan so that a
    missing confirmation button (e.g. message deleted after restart) gets reposted."""
    sub = await _add_pending_thread(
        session, board, state=SubmissionState.READY_TO_QUEUE.value, thread_id=888,
    )
    thread = MagicMock()
    install_history(thread, [])
    repost_bot._resolve_channel = AsyncMock(return_value=thread)
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch("bot.discord_ingest.client.asyncio.sleep", new_callable=AsyncMock),
        patch("bot.discord_ingest.client.service.recompute_and_request", new_callable=AsyncMock) as recompute,
        patch("bot.discord_ingest.client.service._fire_and_forget", side_effect=lambda coro: coro.close()),
    ):
        await repost_bot._run_thread_catchup()
    recompute.assert_awaited_once()
    assert recompute.await_args.args[1].id == sub.id


async def test_thread_catchup_unarchives_thread_before_recompute(repost_bot, session, board):
    """An archived thread must be unarchived before recompute so that sends succeed.
    Without this, the confirmation button repost silently fails."""
    await _add_pending_thread(session, board, thread_id=889)
    thread = MagicMock(spec=discord.Thread)
    thread.archived = True
    install_history(thread, [])
    repost_bot._resolve_channel = AsyncMock(return_value=thread)
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch("bot.discord_ingest.client.asyncio.sleep", new_callable=AsyncMock),
        patch("bot.discord_ingest.client.service._unarchive_thread", new_callable=AsyncMock) as unarchive,
        patch("bot.discord_ingest.client.service.recompute_and_request", new_callable=AsyncMock) as recompute,
        patch("bot.discord_ingest.client.service._fire_and_forget", side_effect=lambda coro: coro.close()),
    ):
        await repost_bot._run_thread_catchup()
    unarchive.assert_awaited_once_with(thread)
    recompute.assert_awaited_once()


# ---------------------------------------------------------------------------
# _archive_queued_threads
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("playlist_ready", "expected_scheduled"), [(True, 2), (False, 1)])
async def test_archive_queued_threads_scheduling(
    repost_bot, session, board, playlist_ready, expected_scheduled
):
    # One QUEUED submission (needs the playlist check, recent so the archive is
    # delayed) and one PUBLISHED (always closeable, old enough to be due now).
    await _add_pending_thread(
        session, board, source_msg_id=1, thread_id=701, state=SubmissionState.QUEUED.value
    )
    await _add_pending_thread(
        session, board, source_msg_id=2, thread_id=702, state=SubmissionState.PUBLISHED.value,
        updated_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    fired = []
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch(
            "bot.discord_ingest.client.service._playlist_close_ready",
            new_callable=AsyncMock, return_value=playlist_ready,
        ) as ready_check,
        patch(
            "bot.discord_ingest.client.service._fire_and_forget",
            side_effect=lambda coro: (fired.append(coro), coro.close()),
        ),
    ):
        await repost_bot._archive_queued_threads()
    ready_check.assert_awaited_once()  # only the QUEUED row is checked
    assert len(fired) == expected_scheduled


async def test_archive_queued_threads_missing_or_aware_timestamps(repost_bot, session):
    # SQLite normally returns naive datetimes; feed handcrafted rows to cover
    # the missing-timestamp (immediate archive) and tz-aware paths.
    rows = [
        SimpleNamespace(
            thread_id=701, updated_at=None, board_id=1,
            source_discord_message_id=1, channel_id=100,
            playlist_skipped=False, state=SubmissionState.PUBLISHED.value,
        ),
        SimpleNamespace(
            thread_id=702, updated_at=datetime.now(timezone.utc), board_id=1,
            source_discord_message_id=2, channel_id=100,
            playlist_skipped=False, state=SubmissionState.PUBLISHED.value,
        ),
    ]
    fired = []
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch.object(session, "execute", new=AsyncMock(return_value=rows)),
        patch(
            "bot.discord_ingest.client.service._fire_and_forget",
            side_effect=lambda coro: (fired.append(coro), coro.close()),
        ),
    ):
        await repost_bot._archive_queued_threads()
    assert len(fired) == 2


# ---------------------------------------------------------------------------
# _archive_thread_by_id
# ---------------------------------------------------------------------------


async def test_archive_thread_by_id_missing_thread_noop(repost_bot):
    repost_bot._resolve_channel = AsyncMock(return_value=None)
    await repost_bot._archive_thread_by_id(777, 0.0)  # must not raise


async def test_archive_thread_by_id_already_archived_skips_edit(repost_bot):
    thread = MagicMock(spec=discord.Thread)
    thread.archived = True
    repost_bot._resolve_channel = AsyncMock(return_value=thread)
    await repost_bot._archive_thread_by_id(777, 0.0, notice="closing")
    thread.send.assert_not_awaited()
    thread.edit.assert_not_awaited()


async def test_archive_thread_by_id_sends_notice_and_archives(repost_bot):
    thread = MagicMock(spec=discord.Thread)
    thread.archived = False
    repost_bot._resolve_channel = AsyncMock(return_value=thread)
    with patch("bot.discord_ingest.client.asyncio.sleep", new_callable=AsyncMock) as sleep:
        await repost_bot._archive_thread_by_id(777, 5.0, notice="closing")
    sleep.assert_awaited_once_with(5.0)
    thread.send.assert_awaited_once_with("closing")
    thread.edit.assert_awaited_once_with(archived=True)


async def test_archive_thread_by_id_notice_failure_still_archives(repost_bot):
    thread = MagicMock(spec=discord.Thread)
    thread.archived = False
    thread.send = AsyncMock(side_effect=RuntimeError("cannot send"))
    repost_bot._resolve_channel = AsyncMock(return_value=thread)
    await repost_bot._archive_thread_by_id(777, 0.0, notice="closing")
    thread.edit.assert_awaited_once_with(archived=True)


async def test_archive_thread_by_id_edit_failure_logged_not_raised(repost_bot):
    thread = MagicMock(spec=discord.Thread)
    thread.archived = False
    thread.edit = AsyncMock(side_effect=_not_found())
    repost_bot._resolve_channel = AsyncMock(return_value=thread)
    await repost_bot._archive_thread_by_id(777, 0.0)  # must not raise


# ---------------------------------------------------------------------------
# _run_threadless_retry_loop
# ---------------------------------------------------------------------------


class _StopLoop(Exception):
    pass


def make_retry_sleep(interval=3 * 60):
    """Fake asyncio.sleep that lets exactly one retry-loop iteration run."""
    outer_calls = 0

    async def _sleep(delay):
        nonlocal outer_calls
        if delay == interval:
            outer_calls += 1
            if outer_calls > 1:
                raise _StopLoop

    return _sleep


async def test_threadless_retry_creates_missing_thread(repost_bot, session, board):
    sub = make_submission(board, source_discord_message_id=5)
    session.add(sub)
    await session.flush()
    message = MagicMock()
    repost_bot._fetch_message = AsyncMock(return_value=message)
    thread = MagicMock()
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch("bot.discord_ingest.client.asyncio.sleep", make_retry_sleep()),
        patch(
            "bot.discord_ingest.client.service._ensure_thread",
            new_callable=AsyncMock, return_value=(thread, False),
        ) as ensure,
        patch("bot.discord_ingest.client.service.recompute_and_request", new_callable=AsyncMock) as recompute,
    ):
        with pytest.raises(_StopLoop):
            await repost_bot._run_threadless_retry_loop()
    ensure.assert_awaited_once()
    recompute.assert_awaited_once()
    assert recompute.await_args.kwargs["destination"] is thread


async def test_threadless_retry_source_message_gone(repost_bot, session, board):
    sub = make_submission(board, source_discord_message_id=5)
    session.add(sub)
    await session.flush()
    repost_bot._fetch_message = AsyncMock(return_value=None)
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch("bot.discord_ingest.client.asyncio.sleep", make_retry_sleep()),
        patch("bot.discord_ingest.client.service._ensure_thread", new_callable=AsyncMock) as ensure,
    ):
        with pytest.raises(_StopLoop):
            await repost_bot._run_threadless_retry_loop()
    ensure.assert_not_awaited()


async def test_threadless_retry_nothing_pending(repost_bot, session):
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch("bot.discord_ingest.client.asyncio.sleep", make_retry_sleep()),
        patch("bot.discord_ingest.client.service._ensure_thread", new_callable=AsyncMock) as ensure,
    ):
        with pytest.raises(_StopLoop):
            await repost_bot._run_threadless_retry_loop()
    ensure.assert_not_awaited()


async def test_threadless_retry_thread_creation_still_failing(repost_bot, session, board):
    sub = make_submission(board, source_discord_message_id=5)
    session.add(sub)
    await session.flush()
    repost_bot._fetch_message = AsyncMock(return_value=MagicMock())
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch("bot.discord_ingest.client.asyncio.sleep", make_retry_sleep()),
        patch(
            "bot.discord_ingest.client.service._ensure_thread",
            new_callable=AsyncMock, return_value=(None, False),
        ),
        patch("bot.discord_ingest.client.service.recompute_and_request", new_callable=AsyncMock) as recompute,
    ):
        with pytest.raises(_StopLoop):
            await repost_bot._run_threadless_retry_loop()
    recompute.assert_not_awaited()


async def test_threadless_retry_skips_submission_gone_by_retry_time(repost_bot, session, board):
    # The fresh re-fetch inside the loop can come back empty (row deleted or
    # published between the scan and the retry); the submission is skipped.
    sub = make_submission(board, source_discord_message_id=5)
    session.add(sub)
    await session.flush()
    repost_bot._fetch_message = AsyncMock(return_value=MagicMock())
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch("bot.discord_ingest.client.asyncio.sleep", make_retry_sleep()),
        patch.object(session, "get", new=AsyncMock(return_value=None)),
        patch("bot.discord_ingest.client.service._ensure_thread", new_callable=AsyncMock) as ensure,
    ):
        with pytest.raises(_StopLoop):
            await repost_bot._run_threadless_retry_loop()
    ensure.assert_not_awaited()


async def test_threadless_retry_survives_iteration_errors(repost_bot):
    with (
        patch(
            "bot.discord_ingest.client.session_scope",
            MagicMock(side_effect=RuntimeError("db down")),
        ),
        patch("bot.discord_ingest.client.asyncio.sleep", make_retry_sleep()),
        patch("bot.discord_ingest.client.log") as mock_log,
    ):
        with pytest.raises(_StopLoop):
            await repost_bot._run_threadless_retry_loop()
    mock_log.exception.assert_called_once()


# ---------------------------------------------------------------------------
# _resolve_channel / _fetch_message / _is_watched_location
# ---------------------------------------------------------------------------


async def test_resolve_channel_cache_hit(repost_bot):
    channel = MagicMock()
    repost_bot.get_channel = MagicMock(return_value=channel)
    repost_bot.fetch_channel = AsyncMock()
    assert await repost_bot._resolve_channel(100) is channel
    repost_bot.fetch_channel.assert_not_awaited()


async def test_resolve_channel_fetch_fallback(repost_bot):
    channel = MagicMock()
    repost_bot.get_channel = MagicMock(return_value=None)
    repost_bot.fetch_channel = AsyncMock(return_value=channel)
    assert await repost_bot._resolve_channel(100) is channel
    repost_bot.fetch_channel.assert_awaited_once_with(100)


async def test_resolve_channel_fetch_failure_returns_none(repost_bot):
    repost_bot.get_channel = MagicMock(return_value=None)
    repost_bot.fetch_channel = AsyncMock(side_effect=_not_found())
    assert await repost_bot._resolve_channel(100) is None


async def test_fetch_message_ok(repost_bot):
    message = MagicMock()
    channel = MagicMock()
    channel.fetch_message = AsyncMock(return_value=message)
    repost_bot._resolve_channel = AsyncMock(return_value=channel)
    assert await repost_bot._fetch_message(100, 555) is message
    channel.fetch_message.assert_awaited_once_with(555)


async def test_fetch_message_not_found_returns_none(repost_bot):
    channel = MagicMock()
    channel.fetch_message = AsyncMock(side_effect=_not_found())
    repost_bot._resolve_channel = AsyncMock(return_value=channel)
    assert await repost_bot._fetch_message(100, 555) is None


async def test_fetch_message_unresolvable_channel_returns_none(repost_bot):
    repost_bot._resolve_channel = AsyncMock(return_value=None)
    assert await repost_bot._fetch_message(100, 555) is None


def test_is_watched_location(repost_bot):
    watched = MagicMock()
    watched.id = 100
    assert repost_bot._is_watched_location(watched) is True

    thread = MagicMock(spec=discord.Thread)
    thread.id = 555
    thread.parent_id = 100
    assert repost_bot._is_watched_location(thread) is True

    stranger = MagicMock()
    stranger.id = 999
    assert repost_bot._is_watched_location(stranger) is False
