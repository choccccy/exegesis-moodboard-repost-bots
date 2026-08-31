"""Query-level tests for the feed generator (bot.feedgen.queries)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot.feedgen.queries import (
    decode_cursor,
    encode_cursor,
    feed_skeleton,
    interleave_by_board,
)
from bot.models import Board, PublishAttempt, SubmissionLink
from bot.state import SubmissionState
from conftest import make_submission

_T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


async def _seed(session, board, *, msg_id, at_uri, minute, success=True,
                error=None, source_at_uri=None, link=False):
    sub = make_submission(
        board, state=SubmissionState.PUBLISHED.value, source_discord_message_id=msg_id
    )
    session.add(sub)
    await session.flush()
    if link or source_at_uri is not None:
        session.add(SubmissionLink(
            submission_id=sub.id,
            order_index=0,
            raw_url="https://bsky.app/x",
            canonical_url="https://bsky.app/profile/someone/post/abc",
            domain_family="bluesky",
            source_at_uri=source_at_uri,
        ))
    session.add(PublishAttempt(
        submission_id=sub.id,
        success=success,
        error=error,
        at_uri=at_uri,
        attempted_at=_T0 + timedelta(minutes=minute),
    ))
    await session.flush()
    return sub


@pytest.fixture
async def nsfw_board(session):
    b = Board(name="xxx-robots", discord_guild_id=1, discord_channel_id=200, nsfw=True)
    session.add(b)
    await session.flush()
    return b


# --- pure helper -----------------------------------------------------------


def test_interleave_fairness():
    # Board A dominates by time; B has a single (older) post. Interleave must surface
    # B's post early instead of burying it behind all of A's.
    items = [("a1", "A"), ("a2", "A"), ("a3", "A"), ("b1", "B")]
    assert interleave_by_board(items) == ["a1", "b1", "a2", "a3"]


def test_interleave_empty():
    assert interleave_by_board([]) == []


def test_cursor_roundtrip_naive_utc():
    ts, uri = decode_cursor(encode_cursor(_T0, "at://did/app.bsky.feed.post/1"))
    assert ts == _T0.replace(tzinfo=None)
    assert uri == "at://did/app.bsky.feed.post/1"


# --- skeleton query --------------------------------------------------------


async def test_only_successful_valid_rows(session, board):
    await _seed(session, board, msg_id=1, at_uri="at://d/app.bsky.feed.post/ok", minute=1)
    await _seed(session, board, msg_id=2, at_uri="at://d/app.bsky.feed.post/fail",
                minute=2, success=False)
    await _seed(session, board, msg_id=3, at_uri="at://d/app.bsky.feed.post/err",
                minute=3, error="boom")
    await _seed(session, board, msg_id=4, at_uri=None, minute=4)  # null at_uri

    uris, cursor = await feed_skeleton(session, nsfw_allowed=True, limit=50)
    assert uris == ["at://d/app.bsky.feed.post/ok"]
    assert cursor is None


async def test_repost_resolves_to_source_and_skips_null(session, board):
    # Repost with a pinned original -> emits the original post URI.
    await _seed(session, board, msg_id=1,
                at_uri="at://bot/app.bsky.feed.repost/r1", minute=2,
                source_at_uri="at://orig/app.bsky.feed.post/p1")
    # Repost missing source_at_uri -> excluded.
    await _seed(session, board, msg_id=2,
                at_uri="at://bot/app.bsky.feed.repost/r2", minute=1, link=True)

    uris, _ = await feed_skeleton(session, nsfw_allowed=True, limit=50)
    assert uris == ["at://orig/app.bsky.feed.post/p1"]


async def test_sfw_excludes_nsfw_boards(session, board, nsfw_board):
    await _seed(session, board, msg_id=1, at_uri="at://d/app.bsky.feed.post/sfw", minute=1)
    await _seed(session, nsfw_board, msg_id=2, at_uri="at://d/app.bsky.feed.post/nsfw", minute=2)

    all_uris, _ = await feed_skeleton(session, nsfw_allowed=True, limit=50)
    sfw_uris, _ = await feed_skeleton(session, nsfw_allowed=False, limit=50)

    assert set(all_uris) == {"at://d/app.bsky.feed.post/sfw", "at://d/app.bsky.feed.post/nsfw"}
    assert sfw_uris == ["at://d/app.bsky.feed.post/sfw"]


async def test_pagination_no_skip_or_dupe(session, board):
    for i in range(5):
        await _seed(session, board, msg_id=i, at_uri=f"at://d/app.bsky.feed.post/{i}", minute=i)

    seen: list[str] = []
    cursor = None
    for _ in range(10):  # generous guard against an infinite loop
        page, cursor = await feed_skeleton(session, nsfw_allowed=True, limit=2, cursor=cursor)
        seen.extend(page)
        if cursor is None:
            break

    # All five, each once, newest-first (minute 4 -> 0).
    assert seen == [f"at://d/app.bsky.feed.post/{i}" for i in (4, 3, 2, 1, 0)]
    assert len(seen) == len(set(seen)) == 5
    assert cursor is None
