"""HTTP-level tests for the read-only feed generator (bot.feedgen).

Same approach as test_dashboard_http: httpx.ASGITransport (shares the aiosqlite loop,
skips lifespan) with settings installed by a fixture; _lifespan is exercised directly.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import httpx
import pytest

from bot.feedgen import _lifespan, app
from bot.feedgen.feeds import FEEDS
from bot.feedgen.settings import FeedgenSettings
from bot.models import Board, PublishAttempt
from bot.state import SubmissionState
from conftest import make_submission

_OWNER_DID = "did:plc:testowner"
_HOSTNAME = "feed.test.example"
_SENTINEL = object()


@pytest.fixture
def feed_settings(global_engine, monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("FEEDGEN_OWNER_DID", _OWNER_DID)
    monkeypatch.setenv("FEEDGEN_SERVICE_HOSTNAME", _HOSTNAME)
    settings = FeedgenSettings()
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


def _feed_uri(rkey: str) -> str:
    return f"at://{_OWNER_DID}/app.bsky.feed.generator/{rkey}"


async def _seed_posts(db, count: int) -> None:
    async with db.session_scope() as session:
        board = Board(name="robots", discord_guild_id=1, discord_channel_id=100)
        session.add(board)
        await session.flush()
        for i in range(count):
            sub = make_submission(
                board, state=SubmissionState.PUBLISHED.value, source_discord_message_id=i
            )
            session.add(sub)
            await session.flush()
            session.add(PublishAttempt(
                submission_id=sub.id,
                success=True,
                error=None,
                at_uri=f"at://d/app.bsky.feed.post/{i}",
                attempted_at=datetime(2026, 1, 1, 12, i, tzinfo=timezone.utc),
            ))


async def test_did_document(global_engine, feed_settings):
    resp = await _get("/.well-known/did.json")
    assert resp.status_code == 200
    doc = resp.json()
    assert doc["id"] == f"did:web:{_HOSTNAME}"
    assert doc["service"][0]["serviceEndpoint"] == f"https://{_HOSTNAME}"
    assert doc["service"][0]["type"] == "BskyFeedGenerator"


async def test_describe_feed_generator(global_engine, feed_settings):
    resp = await _get("/xrpc/app.bsky.feed.describeFeedGenerator")
    assert resp.status_code == 200
    body = resp.json()
    assert body["did"] == f"did:web:{_HOSTNAME}"
    uris = {f["uri"] for f in body["feeds"]}
    assert uris == {_feed_uri(rkey) for rkey in FEEDS}


async def test_get_feed_skeleton_returns_posts(global_engine, feed_settings):
    await _seed_posts(global_engine, 1)
    resp = await _get(
        "/xrpc/app.bsky.feed.getFeedSkeleton",
        params={"feed": _feed_uri("exegesis_moodboards")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["feed"] == [{"post": "at://d/app.bsky.feed.post/0"}]
    assert "cursor" not in body  # single page, no more


async def test_get_feed_skeleton_paginates(global_engine, feed_settings):
    await _seed_posts(global_engine, 2)
    first = await _get(
        "/xrpc/app.bsky.feed.getFeedSkeleton",
        params={"feed": _feed_uri("exegesis_moodboards"), "limit": 1},
    )
    assert first.status_code == 200
    body1 = first.json()
    assert body1["feed"] == [{"post": "at://d/app.bsky.feed.post/1"}]  # newest first
    assert "cursor" in body1

    second = await _get(
        "/xrpc/app.bsky.feed.getFeedSkeleton",
        params={"feed": _feed_uri("exegesis_moodboards"), "limit": 1, "cursor": body1["cursor"]},
    )
    body2 = second.json()
    assert body2["feed"] == [{"post": "at://d/app.bsky.feed.post/0"}]


async def test_get_feed_skeleton_unknown_feed(global_engine, feed_settings):
    resp = await _get(
        "/xrpc/app.bsky.feed.getFeedSkeleton",
        params={"feed": _feed_uri("nope")},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "UnknownFeed"


async def test_get_feed_skeleton_bad_cursor(global_engine, feed_settings):
    resp = await _get(
        "/xrpc/app.bsky.feed.getFeedSkeleton",
        params={"feed": _feed_uri("exegesis_moodboards"), "cursor": "!!!not-base64!!!"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "InvalidRequest"


async def test_lifespan_builds_settings_and_inits_engine(monkeypatch, tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path}/lifespan.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("FEEDGEN_OWNER_DID", _OWNER_DID)

    previous = getattr(app.state, "settings", _SENTINEL)
    with patch("bot.feedgen.init_engine") as init_mock:
        async with _lifespan(app):
            assert isinstance(app.state.settings, FeedgenSettings)
            assert app.state.settings.database_url == db_url
            init_mock.assert_called_once_with(db_url)
    if previous is _SENTINEL:
        del app.state.settings
    else:
        app.state.settings = previous
