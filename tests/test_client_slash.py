"""Tests for RepostBot slash commands and lifecycle hooks.

Covers setup_hook (board sync, slash command registration, per-guild tree
sync including Forbidden/HTTPException guards, and the registered command
closures), _handle_scan_slash and _handle_triage_slash gating and happy
paths, on_ready catch-up task scheduling idempotency, and close().
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from conftest import bound_session_scope, make_interaction


def _http_error(exc_cls, message: str):
    return exc_cls(MagicMock(status=403, reason="Forbidden"), message)


# ---------------------------------------------------------------------------
# setup_hook
# ---------------------------------------------------------------------------


async def test_setup_hook_syncs_boards_and_commands(repost_bot, session):
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch("bot.discord_ingest.client.service.sync_boards", new_callable=AsyncMock) as sync_boards,
        patch.object(repost_bot.tree, "sync", new_callable=AsyncMock, return_value=[]) as tree_sync,
    ):
        await repost_bot.setup_hook()
    try:
        sync_boards.assert_awaited_once()
        tree_sync.assert_awaited_once()  # one configured guild
        assert repost_bot.tree.get_command("scan") is not None
        assert repost_bot.tree.get_command("triage") is not None
        assert repost_bot._http is not None
    finally:
        await repost_bot._http.aclose()


async def test_setup_hook_registered_closures_delegate_to_handlers(repost_bot, session):
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch("bot.discord_ingest.client.service.sync_boards", new_callable=AsyncMock),
        patch.object(repost_bot.tree, "sync", new_callable=AsyncMock, return_value=[]),
    ):
        await repost_bot.setup_hook()
    try:
        repost_bot._handle_scan_slash = AsyncMock()
        repost_bot._handle_triage_slash = AsyncMock()
        interaction = make_interaction()

        scan_cmd = repost_bot.tree.get_command("scan")
        await scan_cmd.callback(interaction, days=30)
        repost_bot._handle_scan_slash.assert_awaited_once_with(interaction, 30)

        triage_cmd = repost_bot.tree.get_command("triage")
        await triage_cmd.callback(interaction)
        repost_bot._handle_triage_slash.assert_awaited_once_with(interaction, None, None)
    finally:
        await repost_bot._http.aclose()


async def test_setup_hook_logs_forbidden_sync_error(repost_bot, session):
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch("bot.discord_ingest.client.service.sync_boards", new_callable=AsyncMock),
        patch.object(
            repost_bot.tree, "sync", new_callable=AsyncMock,
            side_effect=_http_error(discord.Forbidden, "missing scope"),
        ),
        patch("bot.discord_ingest.client.log") as mock_log,
    ):
        await repost_bot.setup_hook()  # must not raise
    await repost_bot._http.aclose()
    assert any(
        "cannot sync" in call.args[0] for call in mock_log.error.call_args_list
    )


async def test_setup_hook_logs_http_sync_error(repost_bot, session):
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch("bot.discord_ingest.client.service.sync_boards", new_callable=AsyncMock),
        patch.object(
            repost_bot.tree, "sync", new_callable=AsyncMock,
            side_effect=_http_error(discord.HTTPException, "rate limited"),
        ),
        patch("bot.discord_ingest.client.log") as mock_log,
    ):
        await repost_bot.setup_hook()  # must not raise
    await repost_bot._http.aclose()
    assert any(
        "HTTP error" in call.args[0] for call in mock_log.error.call_args_list
    )


# ---------------------------------------------------------------------------
# _handle_scan_slash
# ---------------------------------------------------------------------------


async def test_scan_slash_refuses_unwatched_channel(repost_bot):
    interaction = make_interaction(channel_id=999)
    interaction.response.send_message = AsyncMock()
    await repost_bot._handle_scan_slash(interaction, 30)
    interaction.response.send_message.assert_awaited_once()
    assert "watched" in interaction.response.send_message.await_args.args[0]


async def test_scan_slash_refuses_non_curator(repost_bot):
    interaction = make_interaction(channel_id=100)
    interaction.response.send_message = AsyncMock()
    with patch("bot.discord_ingest.client.service._is_curator", return_value=False):
        await repost_bot._handle_scan_slash(interaction, 30)
    interaction.response.send_message.assert_awaited_once()
    assert "curator" in interaction.response.send_message.await_args.args[0]


async def test_scan_slash_defers_and_starts_scan_task(repost_bot):
    interaction = make_interaction(channel_id=100)
    status_message = MagicMock()
    interaction.followup.send = AsyncMock(return_value=status_message)
    repost_bot._run_channel_scan = AsyncMock()
    with patch("bot.discord_ingest.client.service._is_curator", return_value=True):
        await repost_bot._handle_scan_slash(interaction, 30)
        await asyncio.sleep(0)  # let the created task run
    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    repost_bot._run_channel_scan.assert_awaited_once()
    args = repost_bot._run_channel_scan.await_args.args
    assert args[0] is interaction.channel
    assert args[2] is status_message


# ---------------------------------------------------------------------------
# _handle_triage_slash
# ---------------------------------------------------------------------------


async def test_triage_slash_with_filter_sends_filtered_list(repost_bot, session):
    interaction = make_interaction(channel_id=100)
    interaction.channel.name = "robots"
    choice = MagicMock()
    choice.value = "queued"
    choice.name = "queued"
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch("bot.discord_ingest.client.service._is_curator", return_value=True),
        patch(
            "bot.discord_ingest.client.service._board_for_channel",
            new_callable=AsyncMock, return_value=SimpleNamespace(id=1),
        ),
        patch(
            "bot.discord_ingest.client.service.fetch_triage_items",
            new_callable=AsyncMock, return_value=[],
        ) as fetch_items,
    ):
        await repost_bot._handle_triage_slash(interaction, choice)
    assert fetch_items.await_args.kwargs["state_filter"] == "queued"
    content = interaction.followup.send.await_args.args[0]
    assert "All clear" in content
    assert "matching 'queued'" in content


async def test_triage_slash_without_filter_groups_all_items(repost_bot, session):
    interaction = make_interaction(channel_id=100)
    interaction.channel.name = "robots"
    items = [
        SimpleNamespace(
            title="A robot", thread_url=None, author_display="osi",
            submitted_rel="1 day ago", state="awaiting_source",
        ),
    ]
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch("bot.discord_ingest.client.service._is_curator", return_value=True),
        patch(
            "bot.discord_ingest.client.service._board_for_channel",
            new_callable=AsyncMock, return_value=SimpleNamespace(id=1),
        ),
        patch(
            "bot.discord_ingest.client.service.fetch_triage_items",
            new_callable=AsyncMock, return_value=items,
        ) as fetch_items,
    ):
        await repost_bot._handle_triage_slash(interaction, None)
    assert fetch_items.await_args.kwargs["state_filter"] is None
    content = interaction.followup.send.await_args.args[0]
    assert "Awaiting source" in content
    assert "A robot" in content


async def test_triage_slash_with_user_filter_threads_user_id(repost_bot, session):
    interaction = make_interaction(channel_id=100)
    interaction.channel.name = "robots"
    user = MagicMock()
    user.id = 424242
    user.display_name = "osi"
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch("bot.discord_ingest.client.service._is_curator", return_value=True),
        patch(
            "bot.discord_ingest.client.service._board_for_channel",
            new_callable=AsyncMock, return_value=SimpleNamespace(id=1),
        ),
        patch(
            "bot.discord_ingest.client.service.fetch_triage_items",
            new_callable=AsyncMock, return_value=[],
        ) as fetch_items,
    ):
        await repost_bot._handle_triage_slash(interaction, None, user)
    assert fetch_items.await_args.kwargs["user_id_filter"] == 424242
    content = interaction.followup.send.await_args.args[0]
    assert "from osi" in content  # empty-result message names the submitter


async def test_triage_slash_combines_state_and_user_filters(repost_bot, session):
    interaction = make_interaction(channel_id=100)
    interaction.channel.name = "robots"
    choice = MagicMock()
    choice.value = "queued"
    choice.name = "queued"
    user = MagicMock()
    user.id = 555
    user.display_name = "curator"
    items = [
        SimpleNamespace(
            title="A robot", thread_url=None, author_display="curator",
            submitted_rel="1 day ago", state="queued",
        ),
    ]
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch("bot.discord_ingest.client.service._is_curator", return_value=True),
        patch(
            "bot.discord_ingest.client.service._board_for_channel",
            new_callable=AsyncMock, return_value=SimpleNamespace(id=1),
        ),
        patch(
            "bot.discord_ingest.client.service.fetch_triage_items",
            new_callable=AsyncMock, return_value=items,
        ) as fetch_items,
    ):
        await repost_bot._handle_triage_slash(interaction, choice, user)
    assert fetch_items.await_args.kwargs["state_filter"] == "queued"
    assert fetch_items.await_args.kwargs["user_id_filter"] == 555
    content = interaction.followup.send.await_args.args[0]
    assert "queued" in content
    assert "by curator" in content


async def test_triage_slash_board_missing_reports_error(repost_bot, session):
    interaction = make_interaction(channel_id=100)
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch("bot.discord_ingest.client.service._is_curator", return_value=True),
        patch(
            "bot.discord_ingest.client.service._board_for_channel",
            new_callable=AsyncMock, return_value=None,
        ),
    ):
        await repost_bot._handle_triage_slash(interaction, None)
    interaction.followup.send.assert_awaited_once_with("Board not found.", ephemeral=True)


async def test_triage_slash_refuses_non_curator(repost_bot):
    interaction = make_interaction(channel_id=100)
    interaction.response.send_message = AsyncMock()
    with patch("bot.discord_ingest.client.service._is_curator", return_value=False):
        await repost_bot._handle_triage_slash(interaction, None)
    assert "curator" in interaction.response.send_message.await_args.args[0]


async def test_triage_slash_refuses_unwatched_channel(repost_bot):
    interaction = make_interaction(channel_id=999)
    interaction.response.send_message = AsyncMock()
    await repost_bot._handle_triage_slash(interaction, None)
    assert "watched" in interaction.response.send_message.await_args.args[0]


# ---------------------------------------------------------------------------
# on_ready
# ---------------------------------------------------------------------------


async def test_on_ready_catchup_disabled_starts_nothing(repost_bot):
    repost_bot._run_catchup = AsyncMock()
    repost_bot._run_threadless_retry_loop = AsyncMock()
    await repost_bot.on_ready()
    await asyncio.sleep(0)
    repost_bot._run_catchup.assert_not_awaited()
    repost_bot._run_threadless_retry_loop.assert_not_awaited()
    assert repost_bot._catchup_started is False


async def test_on_ready_catchup_enabled_runs_once(repost_bot):
    repost_bot.settings.catchup_enabled = True
    repost_bot._run_catchup = AsyncMock()
    repost_bot._run_threadless_retry_loop = AsyncMock()

    await repost_bot.on_ready()
    await asyncio.sleep(0)
    repost_bot._run_catchup.assert_awaited_once()
    repost_bot._run_threadless_retry_loop.assert_awaited_once()

    # Reconnect fires on_ready again; catch-up must not restart.
    await repost_bot.on_ready()
    await asyncio.sleep(0)
    repost_bot._run_catchup.assert_awaited_once()


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


async def test_close_shuts_down_httpx_client(repost_bot):
    http = AsyncMock()
    repost_bot._http = http
    with patch("discord.Client.close", new_callable=AsyncMock) as super_close:
        await repost_bot.close()
    http.aclose.assert_awaited_once()
    super_close.assert_awaited_once()


async def test_close_without_httpx_client(repost_bot):
    assert repost_bot._http is None
    with patch("discord.Client.close", new_callable=AsyncMock) as super_close:
        await repost_bot.close()
    super_close.assert_awaited_once()


# ---------------------------------------------------------------------------
# /status - in-thread submission status
# ---------------------------------------------------------------------------


def _status_interaction(user_id=999):
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = MagicMock()
    interaction.user.id = user_id
    interaction.channel = MagicMock()
    interaction.channel.id = 5000  # thread id
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    return interaction


async def _seed_thread_submission(session, board, *, author_id=999, state=None):
    from bot.models import Submission, SubmissionThread, SubmissionLink
    from bot.state import SubmissionState
    sub = Submission(
        board_id=board.id, source_discord_message_id=8888, channel_id=board.discord_channel_id,
        author_id=author_id, author_display="op", thread_id=5000,
        state=(state or SubmissionState.AWAITING_SOURCE.value),
    )
    session.add(sub)
    await session.flush()
    session.add(SubmissionThread(
        board_id=board.id, source_discord_message_id=8888, thread_id=5000,
    ))
    await session.flush()
    return sub


async def test_status_slash_in_thread_renders(repost_bot, session, board):
    await _seed_thread_submission(session, board, author_id=999)
    interaction = _status_interaction(user_id=999)
    with patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)):
        await repost_bot._handle_status_slash(interaction)
    content = interaction.response.send_message.await_args.args[0]
    assert "post status" in content
    assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True


async def test_status_slash_not_in_submission_thread(repost_bot, session, board):
    interaction = _status_interaction()
    interaction.channel.id = 999999  # not a submission thread
    with patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)):
        await repost_bot._handle_status_slash(interaction)
    content = interaction.response.send_message.await_args.args[0]
    assert "inside a submission" in content


async def test_status_slash_unauthorized(repost_bot, session, board):
    await _seed_thread_submission(session, board, author_id=1)  # OP is someone else
    interaction = _status_interaction(user_id=555)  # not OP, not curator
    with patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)):
        await repost_bot._handle_status_slash(interaction)
    content = interaction.response.send_message.await_args.args[0]
    assert "not authorised" in content


async def test_status_slash_no_channel(repost_bot, session, board):
    interaction = _status_interaction()
    interaction.channel = None
    with patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)):
        await repost_bot._handle_status_slash(interaction)
    content = interaction.response.send_message.await_args.args[0]
    assert "inside a submission" in content


async def test_status_command_closure_delegates(repost_bot, session):
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch("bot.discord_ingest.client.service.sync_boards", new_callable=AsyncMock),
        patch.object(repost_bot.tree, "sync", new_callable=AsyncMock, return_value=[]),
    ):
        await repost_bot.setup_hook()
    try:
        repost_bot._handle_status_slash = AsyncMock()
        interaction = _status_interaction()
        await repost_bot.tree.get_command("status").callback(interaction)
        repost_bot._handle_status_slash.assert_awaited_once_with(interaction)
    finally:
        await repost_bot._http.aclose()


# ---------------------------------------------------------------------------
# /reingest, /no_source, /skip_alt - thread-scoped commands
# ---------------------------------------------------------------------------


def _thread_interaction(user_id=999, channel_id=5000):
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = MagicMock()
    interaction.user.id = user_id
    interaction.channel = MagicMock()
    interaction.channel.id = channel_id
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


async def test_reingest_slash_happy_path(repost_bot, session, board):
    await _seed_thread_submission(session, board, author_id=999)
    interaction = _thread_interaction(user_id=999)
    repost_bot._fetch_message = AsyncMock(return_value=MagicMock())
    repost_bot._http = MagicMock()  # httpx_client property asserts this is set
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch("bot.discord_ingest.client.service.reingest_submission", new_callable=AsyncMock) as reingest,
        patch("bot.discord_ingest.client.service.recompute_and_request", new_callable=AsyncMock) as recompute,
    ):
        await repost_bot._handle_reingest_slash(interaction)
    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    reingest.assert_awaited_once()
    assert reingest.await_args.kwargs["message"] is repost_bot._fetch_message.return_value
    recompute.assert_awaited_once()
    assert "Reingested" in interaction.followup.send.await_args.args[0]


async def test_reingest_slash_not_in_thread(repost_bot, session, board):
    interaction = _thread_interaction(channel_id=999999)  # not a submission thread
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch("bot.discord_ingest.client.service.reingest_submission", new_callable=AsyncMock) as reingest,
    ):
        await repost_bot._handle_reingest_slash(interaction)
    reingest.assert_not_awaited()
    assert "inside a submission" in interaction.followup.send.await_args.args[0]


async def test_reingest_slash_unauthorized(repost_bot, session, board):
    await _seed_thread_submission(session, board, author_id=1)  # OP is someone else
    interaction = _thread_interaction(user_id=555)
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch("bot.discord_ingest.client.service.reingest_submission", new_callable=AsyncMock) as reingest,
    ):
        await repost_bot._handle_reingest_slash(interaction)
    reingest.assert_not_awaited()
    assert "not authorised" in interaction.followup.send.await_args.args[0]


async def test_reingest_slash_terminal_blocked(repost_bot, session, board):
    from bot.state import SubmissionState
    await _seed_thread_submission(session, board, author_id=999, state=SubmissionState.QUEUED.value)
    interaction = _thread_interaction(user_id=999)
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch("bot.discord_ingest.client.service.reingest_submission", new_callable=AsyncMock) as reingest,
    ):
        await repost_bot._handle_reingest_slash(interaction)
    reingest.assert_not_awaited()
    assert "already queued or published" in interaction.followup.send.await_args.args[0]


async def test_reingest_slash_source_message_deleted(repost_bot, session, board):
    await _seed_thread_submission(session, board, author_id=999)
    interaction = _thread_interaction(user_id=999)
    repost_bot._fetch_message = AsyncMock(return_value=None)  # message gone
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch("bot.discord_ingest.client.service.reingest_submission", new_callable=AsyncMock) as reingest,
    ):
        await repost_bot._handle_reingest_slash(interaction)
    reingest.assert_not_awaited()
    assert "deleted or is unreachable" in interaction.followup.send.await_args.args[0]


async def test_no_source_slash_waives(repost_bot, session, board):
    await _seed_thread_submission(session, board, author_id=999)
    interaction = _thread_interaction(user_id=999)
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch("bot.discord_ingest.client.service.waive_source", new_callable=AsyncMock, return_value=True) as waive,
    ):
        await repost_bot._handle_nosource_slash(interaction)
    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    waive.assert_awaited_once()
    assert waive.await_args.kwargs["user_id"] == 999
    assert "Marked as no known source" in interaction.followup.send.await_args.args[0]


async def test_no_source_slash_already_waived(repost_bot, session, board):
    await _seed_thread_submission(session, board, author_id=999)
    interaction = _thread_interaction(user_id=999)
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch("bot.discord_ingest.client.service.waive_source", new_callable=AsyncMock, return_value=False),
    ):
        await repost_bot._handle_nosource_slash(interaction)
    assert "Already marked" in interaction.followup.send.await_args.args[0]


async def test_no_source_slash_not_in_thread(repost_bot, session, board):
    interaction = _thread_interaction(channel_id=999999)
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch("bot.discord_ingest.client.service.waive_source", new_callable=AsyncMock) as waive,
    ):
        await repost_bot._handle_nosource_slash(interaction)
    waive.assert_not_awaited()
    assert "inside a submission" in interaction.followup.send.await_args.args[0]


async def test_no_source_slash_unauthorized(repost_bot, session, board):
    await _seed_thread_submission(session, board, author_id=1)
    interaction = _thread_interaction(user_id=555)
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch("bot.discord_ingest.client.service.waive_source", new_callable=AsyncMock) as waive,
    ):
        await repost_bot._handle_nosource_slash(interaction)
    waive.assert_not_awaited()
    assert "not authorised" in interaction.followup.send.await_args.args[0]


async def test_skip_alt_slash_skips(repost_bot, session, board):
    await _seed_thread_submission(session, board, author_id=999)
    interaction = _thread_interaction(user_id=999)
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch("bot.discord_ingest.client.service.skip_all_alt_text", new_callable=AsyncMock, return_value=2) as skip,
    ):
        await repost_bot._handle_skipalt_slash(interaction)
    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    skip.assert_awaited_once()
    assert "Skipped alt text for 2 image(s)" in interaction.followup.send.await_args.args[0]


async def test_skip_alt_slash_nothing_pending(repost_bot, session, board):
    await _seed_thread_submission(session, board, author_id=999)
    interaction = _thread_interaction(user_id=999)
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch("bot.discord_ingest.client.service.skip_all_alt_text", new_callable=AsyncMock, return_value=0),
    ):
        await repost_bot._handle_skipalt_slash(interaction)
    assert "No images are waiting" in interaction.followup.send.await_args.args[0]


async def test_skip_alt_slash_not_in_thread(repost_bot, session, board):
    interaction = _thread_interaction(channel_id=999999)
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch("bot.discord_ingest.client.service.skip_all_alt_text", new_callable=AsyncMock) as skip,
    ):
        await repost_bot._handle_skipalt_slash(interaction)
    skip.assert_not_awaited()
    assert "inside a submission" in interaction.followup.send.await_args.args[0]


async def test_reingest_slash_submission_vanished_after_fetch(repost_bot, session, board):
    # Rare race: submission deleted between the auth check and the reingest session.
    sub = await _seed_thread_submission(session, board, author_id=999)
    interaction = _thread_interaction(user_id=999)
    repost_bot._fetch_message = AsyncMock(return_value=MagicMock())
    repost_bot._http = MagicMock()
    repost_bot._submission_for_thread = AsyncMock(side_effect=[sub, None])  # exists, then gone
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch("bot.discord_ingest.client.service.reingest_submission", new_callable=AsyncMock) as reingest,
        patch("bot.discord_ingest.client.service.recompute_and_request", new_callable=AsyncMock),
    ):
        await repost_bot._handle_reingest_slash(interaction)
    reingest.assert_not_awaited()
    assert "no longer exists" in interaction.followup.send.await_args.args[0]


async def test_skip_alt_slash_unauthorized(repost_bot, session, board):
    await _seed_thread_submission(session, board, author_id=1)
    interaction = _thread_interaction(user_id=555)
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch("bot.discord_ingest.client.service.skip_all_alt_text", new_callable=AsyncMock) as skip,
    ):
        await repost_bot._handle_skipalt_slash(interaction)
    skip.assert_not_awaited()
    assert "not authorised" in interaction.followup.send.await_args.args[0]


async def test_new_command_closures_delegate(repost_bot, session):
    with (
        patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)),
        patch("bot.discord_ingest.client.service.sync_boards", new_callable=AsyncMock),
        patch.object(repost_bot.tree, "sync", new_callable=AsyncMock, return_value=[]),
    ):
        await repost_bot.setup_hook()
    try:
        repost_bot._handle_reingest_slash = AsyncMock()
        repost_bot._handle_nosource_slash = AsyncMock()
        repost_bot._handle_skipalt_slash = AsyncMock()
        interaction = _thread_interaction()
        await repost_bot.tree.get_command("reingest").callback(interaction)
        await repost_bot.tree.get_command("no_source").callback(interaction)
        await repost_bot.tree.get_command("skip_alt").callback(interaction)
        repost_bot._handle_reingest_slash.assert_awaited_once_with(interaction)
        repost_bot._handle_nosource_slash.assert_awaited_once_with(interaction)
        repost_bot._handle_skipalt_slash.assert_awaited_once_with(interaction)
    finally:
        await repost_bot._http.aclose()
