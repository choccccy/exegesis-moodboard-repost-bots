"""HTTP-level tests for the read-only dashboard (bot.dashboard).

Uses httpx.ASGITransport rather than a TestClient so every request runs on
the same event loop as the aiosqlite engine set up by the global_engine
fixture. ASGITransport intentionally skips lifespan, so app.state.settings
is installed by the dash_settings fixture; _lifespan itself is exercised
directly in its own test.
"""

from __future__ import annotations

import json
import time
from unittest.mock import patch

import httpx
import pytest

from bot.dashboard import _lifespan, app
from bot.dashboard.settings import DashboardSettings
from bot.models import Board, BotError, PublishAttempt, SubmissionLink, YoutubePlaylistAdd
from bot.state import SubmissionState
from bot.version import __version__
from conftest import make_submission

_BOARDS_JSON = json.dumps([
    {
        "name": "robots",
        "discord_guild_id": 1,
        "discord_channel_id": 100,
        "bluesky_handle": "robots.exegesis.space",
        "youtube_playlist_id": "PLtest123",
    },
])

_SENTINEL = object()


@pytest.fixture
def dash_settings(global_engine, monkeypatch, tmp_path):
    """Env-built DashboardSettings installed on app.state, restored after."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BOARDS_JSON", _BOARDS_JSON)
    settings = DashboardSettings()
    previous = getattr(app.state, "settings", _SENTINEL)
    app.state.settings = settings
    yield settings
    if previous is _SENTINEL:
        del app.state.settings
    else:
        app.state.settings = previous


async def _get(path: str, **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, **kwargs)


async def _seed_board(db) -> int:
    async with db.session_scope() as session:
        board = Board(name="robots", discord_guild_id=1, discord_channel_id=100)
        session.add(board)
        await session.flush()
        return board.id


async def test_index_empty_db(global_engine, dash_settings):
    resp = await _get("/")
    assert resp.status_code == 200
    body = resp.text
    assert f"v{__version__}" in body
    assert "no publishes yet" in body


async def test_index_with_seeded_data(global_engine, dash_settings):
    db = global_engine
    board_id = await _seed_board(db)
    async with db.session_scope() as session:
        board = await session.get(Board, board_id)

        published = make_submission(
            board,
            state=SubmissionState.PUBLISHED.value,
            source_discord_message_id=11,
            author_display="publisher_person",
            embed_title="A very shiny robot",
        )
        pending = make_submission(
            board,
            state=SubmissionState.INTENT_SUBMITTED.value,
            source_discord_message_id=12,
            author_display="pending_person",
        )
        session.add_all([published, pending])
        await session.flush()

        session.add(PublishAttempt(
            submission_id=published.id,
            success=True,
            error=None,
            bsky_url="https://bsky.app/profile/robots.exegesis.space/post/xyz",
        ))
        session.add(YoutubePlaylistAdd(
            board_id=board_id,
            source_discord_message_id=13,
            video_id="dQw4w9WgXcQ",
            playlist_id="PLtest123",
            discord_requester_id=999,
            success=True,
        ))
        session.add(BotError(
            source="scheduler",
            context="board robots",
            traceback="Traceback: KaboomError: it broke",
        ))

    resp = await _get("/")
    assert resp.status_code == 200
    body = resp.text
    # Board stats card
    assert "robots" in body
    assert "robots.exegesis.space" in body
    # Recent publish row
    assert "A very shiny robot" in body
    assert "publisher_person" in body
    # Pending submission with state-fallback blocker
    assert "pending_person" in body
    assert "submitted" in body
    # Playlist add row
    assert "youtu.be/dQw4w9WgXcQ" in body
    # Recent error
    assert "scheduler" in body
    assert "KaboomError: it broke" in body


async def test_index_renders_bot_status(global_engine, dash_settings, tmp_path):
    now = time.time()
    (tmp_path / "bot_status.json").write_text(json.dumps({
        "started_at": "2026-06-24T00:00:00+00:00",
        "discord_active_threads": 42,
        "rate_limit": {
            "until": now + 300,
            "route": "POST https://discord.com/api/foo responded",
            "retry_after": 300,
            "last_seen_at": now,
        },
        "active_scans": [{
            "channel_id": 100,
            "channel_name": "robots",
            "type": "catchup",
            "started_at": now - 60,
        }],
    }))

    resp = await _get("/")
    assert resp.status_code == 200
    body = resp.text
    assert "42/1000" in body
    assert "rate limited until" in body
    assert "catchup: #robots" in body
    assert "up " in body  # bot_started_at rendered


async def test_unknown_board_redirects_to_index(global_engine, dash_settings):
    resp = await _get("/boards/does-not-exist", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/"


async def test_board_queue_renders_items_and_handle(global_engine, dash_settings):
    db = global_engine
    board_id = await _seed_board(db)
    async with db.session_scope() as session:
        board = await session.get(Board, board_id)
        queued = make_submission(
            board,
            state=SubmissionState.QUEUED.value,
            source_discord_message_id=21,
            author_display="queue_person",
            embed_title="Queued robot artwork",
        )
        session.add(queued)
        await session.flush()
        session.add(SubmissionLink(
            submission_id=queued.id,
            order_index=0,
            raw_url="https://example.com/robot",
            canonical_url="https://example.com/robot",
            domain_family="generic",
        ))

    resp = await _get("/boards/robots")
    assert resp.status_code == 200
    body = resp.text
    assert "@robots.exegesis.space" in body
    assert "Queued robot artwork" in body
    assert "https://example.com/robot" in body
    assert "queue_person" in body


async def test_board_queue_shows_failure_error(global_engine, dash_settings):
    db = global_engine
    board_id = await _seed_board(db)
    async with db.session_scope() as session:
        board = await session.get(Board, board_id)
        failed = make_submission(
            board,
            state=SubmissionState.PUBLISH_FAILED.value,
            source_discord_message_id=31,
            author_display="fail_person",
        )
        session.add(failed)
        await session.flush()
        session.add(PublishAttempt(
            submission_id=failed.id,
            success=False,
            error="bsky exploded spectacularly",
        ))

    resp = await _get("/boards/robots")
    assert resp.status_code == 200
    body = resp.text
    assert "bsky exploded spectacularly" in body
    assert "failed" in body


async def test_lifespan_builds_settings_and_inits_engine(monkeypatch, tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path}/lifespan.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BOARDS_JSON", "[]")

    previous = getattr(app.state, "settings", _SENTINEL)
    with patch("bot.dashboard.init_engine") as init_mock:
        async with _lifespan(app):
            assert isinstance(app.state.settings, DashboardSettings)
            assert app.state.settings.database_url == db_url
            init_mock.assert_called_once_with(db_url)
    if previous is _SENTINEL:
        del app.state.settings
    else:
        app.state.settings = previous


def test_bluesky_handle_for_fallbacks(monkeypatch):
    monkeypatch.setenv("BOARDS_JSON", _BOARDS_JSON)
    settings = DashboardSettings()
    assert settings.bluesky_handle_for("robots") == "robots.exegesis.space"
    assert settings.bluesky_handle_for("no-such-board") is None

    monkeypatch.setenv("BOARDS_JSON", "[]")
    empty = DashboardSettings()
    assert empty.boards == []
    assert empty.bluesky_handle_for("robots") is None


async def test_index_repost_type_and_recent_rate_limit(global_engine, dash_settings, tmp_path):
    """Covers the repost post-type branch and the stale rate-limit pill."""
    db = global_engine
    board_id = await _seed_board(db)
    async with db.session_scope() as session:
        board = await session.get(Board, board_id)
        reposted = make_submission(
            board,
            state=SubmissionState.PUBLISHED.value,
            source_discord_message_id=41,
            author_display="repost_person",
        )
        session.add(reposted)
        await session.flush()
        session.add(PublishAttempt(
            submission_id=reposted.id,
            success=True,
            error=None,
            at_uri="at://did:plc:abc/app.bsky.feed.repost/123",
        ))

    now = time.time()
    (tmp_path / "bot_status.json").write_text(json.dumps({
        "rate_limit": {
            "until": now - 30,          # already cleared
            "route": "GET https://discord.com/api/bar responded",
            "last_seen_at": now - 30,   # within the 2-minute window
        },
    }))

    resp = await _get("/")
    assert resp.status_code == 200
    body = resp.text
    assert "repost" in body
    assert "was rate limited recently" in body
    # Repost records are not feed posts, so at_uri_to_url returns the AT URI as-is
    assert "at://did:plc:abc/app.bsky.feed.repost/123" in body
