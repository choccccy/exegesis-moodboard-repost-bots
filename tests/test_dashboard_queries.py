"""Tests for the untested dashboard query functions.

Covers pending_submissions (state buckets, blocker labels, ordering, thread
URLs), recent_playlist_adds, recent_errors, global_stats (including the
bot_status.json rate-limit freshness window), and the _relative helper.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

from bot.dashboard.queries import (
    _relative,
    global_stats,
    pending_submissions,
    recent_errors,
    recent_playlist_adds,
)
from bot.models import (
    BotError,
    ImageRequest,
    MetadataRequest,
    SourceRequest,
    SubmissionLink,
    SubmissionThread,
    YoutubePlaylistAdd,
)
from bot.state import SubmissionState

from conftest import make_submission

QUEUED = SubmissionState.QUEUED.value
PUBLISHED = SubmissionState.PUBLISHED.value


class _StatSettings:
    """Minimal settings object with the attrs global_stats/board_stats read."""

    queue_timezone = "America/Denver"
    queue_fresh_window_hours = 72
    queue_target_days = 90
    queue_min_daily = 1
    queue_max_daily = 6
    boards = []

    def bluesky_handle_for(self, name):
        return f"{name}.exegesis.space"


# ---------------------------------------------------------------------------
# _relative
# ---------------------------------------------------------------------------

def test_relative_none_is_never():
    assert _relative(None) == "never"


def test_relative_just_now():
    assert _relative(datetime.now(timezone.utc)) == "just now"


def test_relative_minutes():
    assert _relative(datetime.now(timezone.utc) - timedelta(minutes=5)) == "5m ago"


def test_relative_hours():
    assert _relative(datetime.now(timezone.utc) - timedelta(hours=3)) == "3h ago"


def test_relative_days():
    assert _relative(datetime.now(timezone.utc) - timedelta(days=2)) == "2d ago"


def test_relative_naive_datetime_treated_as_utc():
    naive = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
    assert _relative(naive) == "1h ago"


# ---------------------------------------------------------------------------
# pending_submissions
# ---------------------------------------------------------------------------

async def test_pending_empty(session, board):
    assert await pending_submissions(session) == []


async def test_pending_includes_only_pending_states(session, board):
    pending = make_submission(
        board, state=SubmissionState.AWAITING_ALT_TEXT.value, source_discord_message_id=1
    )
    ready = make_submission(
        board, state=SubmissionState.READY_TO_QUEUE.value, source_discord_message_id=2
    )
    queued = make_submission(board, state=QUEUED, source_discord_message_id=3)
    published = make_submission(board, state=PUBLISHED, source_discord_message_id=4)
    session.add_all([pending, ready, queued, published])
    await session.flush()

    result = await pending_submissions(session)

    assert [p.submission_id for p in result] == [pending.id]
    assert result[0].board_name == board.name


async def test_pending_state_label_fallback_without_request_rows(session, board):
    sub = make_submission(
        board, state=SubmissionState.AWAITING_SOURCE.value, source_discord_message_id=1
    )
    session.add(sub)
    await session.flush()

    result = await pending_submissions(session)

    assert result[0].blockers == ["needs source"]


async def test_pending_intent_submitted_label(session, board):
    sub = make_submission(
        board, state=SubmissionState.INTENT_SUBMITTED.value, source_discord_message_id=1
    )
    session.add(sub)
    await session.flush()

    result = await pending_submissions(session)

    assert result[0].blockers == ["submitted"]


async def test_pending_blockers_from_open_requests_in_display_order(session, board):
    sub = make_submission(
        board, state=SubmissionState.AWAITING_IMAGE.value, source_discord_message_id=1
    )
    session.add(sub)
    await session.flush()
    # Insert in reverse of display order; output must follow _BLOCKER_ORDER.
    session.add(ImageRequest(submission_id=sub.id, bot_message_id=201))
    session.add(SourceRequest(submission_id=sub.id, bot_message_id=202))
    await session.flush()

    result = await pending_submissions(session)

    assert result[0].blockers == ["needs source", "needs image"]


async def test_pending_answered_requests_do_not_block(session, board):
    sub = make_submission(
        board, state=SubmissionState.AWAITING_BETTER_LINK.value, source_discord_message_id=1
    )
    session.add(sub)
    await session.flush()
    session.add(MetadataRequest(
        submission_id=sub.id, bot_message_id=301,
        answered_at=datetime.now(timezone.utc), answer="confirmed",
    ))
    await session.flush()

    result = await pending_submissions(session)

    # The answered request is ignored; the state label is the fallback.
    assert result[0].blockers == ["needs metadata"]


async def test_pending_ordered_oldest_first(session, board):
    older = make_submission(
        board, state=SubmissionState.AWAITING_SOURCE.value, source_discord_message_id=1,
        created_at=datetime(2026, 6, 1, 12, 0, 0),
    )
    newer = make_submission(
        board, state=SubmissionState.AWAITING_SOURCE.value, source_discord_message_id=2,
        created_at=datetime(2026, 6, 2, 12, 0, 0),
    )
    session.add_all([newer, older])
    await session.flush()

    result = await pending_submissions(session)

    assert [p.submission_id for p in result] == [older.id, newer.id]


async def test_pending_thread_url_and_canonical_url(session, board):
    sub = make_submission(
        board, state=SubmissionState.AWAITING_ALT_TEXT.value, source_discord_message_id=42
    )
    session.add(sub)
    await session.flush()
    session.add(SubmissionLink(
        submission_id=sub.id, order_index=0,
        raw_url="https://example.com/a", canonical_url="https://example.com/a",
        domain_family="other",
    ))
    session.add(SubmissionThread(
        board_id=board.id, source_discord_message_id=42, thread_id=777,
    ))
    await session.flush()

    result = await pending_submissions(session)

    assert result[0].thread_url == f"https://discord.com/channels/{board.discord_guild_id}/777"
    assert result[0].canonical_url == "https://example.com/a"


async def test_pending_no_thread_row_gives_none_url(session, board):
    sub = make_submission(
        board, state=SubmissionState.AWAITING_IMAGE.value, source_discord_message_id=1
    )
    session.add(sub)
    await session.flush()

    result = await pending_submissions(session)

    assert result[0].thread_url is None
    assert result[0].canonical_url is None


# ---------------------------------------------------------------------------
# recent_playlist_adds
# ---------------------------------------------------------------------------

async def test_playlist_adds_fields_and_failure(session, board):
    session.add(YoutubePlaylistAdd(
        board_id=board.id, source_discord_message_id=1, video_id="ok123",
        playlist_id="PL1", discord_requester_id=9, success=True,
        added_at=datetime(2026, 7, 1, 10, 0, 0),
    ))
    session.add(YoutubePlaylistAdd(
        board_id=board.id, source_discord_message_id=2, video_id="bad456",
        playlist_id="PL1", discord_requester_id=9, success=False,
        error_message="quota exceeded",
        added_at=datetime(2026, 7, 1, 11, 0, 0),
    ))
    await session.flush()

    result = await recent_playlist_adds(session)

    assert len(result) == 2
    # Newest first.
    assert result[0].video_id == "bad456"
    assert result[0].success is False
    assert result[0].error_message == "quota exceeded"
    assert result[0].board_name == board.name
    assert result[0].added_at.tzinfo is not None
    assert result[1].video_id == "ok123"
    assert result[1].success is True
    assert result[1].error_message is None
    assert result[1].added_rel.endswith("d ago")


async def test_playlist_adds_respects_limit(session, board):
    for i in range(5):
        session.add(YoutubePlaylistAdd(
            board_id=board.id, source_discord_message_id=i, video_id=f"v{i}",
            playlist_id="PL1", discord_requester_id=9, success=True,
            added_at=datetime(2026, 7, 1, 10, i, 0),
        ))
    await session.flush()

    result = await recent_playlist_adds(session, limit=3)

    assert len(result) == 3
    assert [r.video_id for r in result] == ["v4", "v3", "v2"]


# ---------------------------------------------------------------------------
# recent_errors
# ---------------------------------------------------------------------------

async def test_recent_errors_fields_and_order(session):
    session.add(BotError(
        source="scheduler", context="board robots", traceback="Traceback: older",
        occurred_at=datetime(2026, 7, 1, 10, 0, 0),
    ))
    session.add(BotError(
        source="ingest", context="channel 100", traceback="Traceback: newer",
        occurred_at=datetime(2026, 7, 2, 10, 0, 0),
    ))
    await session.flush()

    result = await recent_errors(session)

    assert len(result) == 2
    assert result[0].source == "ingest"
    assert result[0].traceback == "Traceback: newer"
    assert result[0].occurred_at.tzinfo is not None
    assert result[1].source == "scheduler"
    assert result[1].occurred_rel.endswith("d ago")


async def test_recent_errors_limit(session):
    for i in range(4):
        session.add(BotError(
            source="scheduler", context=f"ctx {i}", traceback=f"tb {i}",
            occurred_at=datetime(2026, 7, 1, 10, i, 0),
        ))
    await session.flush()

    result = await recent_errors(session, limit=2)

    assert [r.context for r in result] == ["ctx 3", "ctx 2"]


# ---------------------------------------------------------------------------
# global_stats
# ---------------------------------------------------------------------------

def _write_status(tmp_path, payload: dict) -> str:
    (tmp_path / "bot_status.json").write_text(json.dumps(payload))
    return str(tmp_path)


async def test_global_stats_no_status_file(session, board, tmp_path):
    queued = make_submission(board, state=QUEUED, source_discord_message_id=1)
    pending = make_submission(
        board, state=SubmissionState.AWAITING_SOURCE.value, source_discord_message_id=2
    )
    session.add_all([queued, pending])
    session.add(SubmissionThread(board_id=board.id, source_discord_message_id=1, thread_id=5))
    session.add(SubmissionThread(
        board_id=board.id, source_discord_message_id=9, thread_id=6,
        archived_at=datetime.now(timezone.utc),
    ))
    await session.flush()

    stats = await global_stats(session, _StatSettings(), str(tmp_path))

    assert stats.total_queued == 1
    assert stats.total_pending == 1
    assert stats.total_today == 0
    # Falls back to the DB count of unarchived threads.
    assert stats.active_thread_count == 1
    assert stats.rate_limited_until is None
    assert stats.rate_limited_recently is False
    assert stats.bot_started_at is None
    assert stats.bot_started_rel == "never"
    assert stats.active_scans == []


async def test_global_stats_prefers_discord_thread_count(session, board, tmp_path):
    session.add(SubmissionThread(board_id=board.id, source_discord_message_id=1, thread_id=5))
    await session.flush()
    data_dir = _write_status(tmp_path, {"discord_active_threads": 42})

    stats = await global_stats(session, _StatSettings(), data_dir)

    assert stats.active_thread_count == 42


async def test_global_stats_active_rate_limit(session, board, tmp_path):
    now = time.time()
    data_dir = _write_status(tmp_path, {
        "rate_limit": {
            "until": now + 300,
            "route": "POST https://discord.com/api/v10/channels",
            "retry_after": 300,
            "last_seen_at": now,
        },
    })

    stats = await global_stats(session, _StatSettings(), data_dir)

    assert stats.rate_limited_until is not None
    assert stats.rate_limited_until > datetime.now(timezone.utc)
    assert stats.rate_limited_route == "POST https://discord.com/api/v10/channels"
    assert stats.rate_limited_recently is True


async def test_global_stats_recently_cleared_rate_limit(session, board, tmp_path):
    # until is in the past, but last_seen_at is within the 2 minute window.
    now = time.time()
    data_dir = _write_status(tmp_path, {
        "rate_limit": {"until": now - 10, "route": "GET /gateway", "last_seen_at": now - 60},
    })

    stats = await global_stats(session, _StatSettings(), data_dir)

    assert stats.rate_limited_until is None
    assert stats.rate_limited_recently is True
    assert stats.rate_limited_route == "GET /gateway"


async def test_global_stats_expired_rate_limit(session, board, tmp_path):
    # Both until and last_seen_at are older than the 2 minute freshness window.
    now = time.time()
    data_dir = _write_status(tmp_path, {
        "rate_limit": {"until": now - 600, "route": "GET /gateway", "last_seen_at": now - 600},
    })

    stats = await global_stats(session, _StatSettings(), data_dir)

    assert stats.rate_limited_until is None
    assert stats.rate_limited_recently is False
    assert stats.rate_limited_route is None


async def test_global_stats_started_at_and_scans(session, board, tmp_path):
    started = datetime.now(timezone.utc) - timedelta(hours=2)
    data_dir = _write_status(tmp_path, {
        "started_at": started.isoformat(),
        "active_scans": [
            {"channel_id": 100, "channel_name": "robots", "type": "manual",
             "started_at": time.time() - 300},
            {},  # missing keys fall back to defaults
        ],
    })

    stats = await global_stats(session, _StatSettings(), data_dir)

    assert stats.bot_started_at == started
    assert stats.bot_started_rel == "2h ago"
    assert len(stats.active_scans) == 2
    assert stats.active_scans[0].channel_id == 100
    assert stats.active_scans[0].channel_name == "robots"
    assert stats.active_scans[0].scan_type == "manual"
    assert stats.active_scans[0].started_rel == "5m ago"
    assert stats.active_scans[1].channel_name == "?"
    assert stats.active_scans[1].scan_type == "catchup"
    assert stats.active_scans[1].started_rel == "never"


async def test_global_stats_invalid_started_at_ignored(session, board, tmp_path):
    data_dir = _write_status(tmp_path, {"started_at": "not-a-date"})

    stats = await global_stats(session, _StatSettings(), data_dir)

    assert stats.bot_started_at is None
    assert stats.bot_started_rel == "never"
