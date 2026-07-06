"""Tests for board sync, channel lookup, and triage listing in service.py.

Covers sync_boards (create/update/idempotency), _board_for_channel, and
fetch_triage_items (state filtering, ordering, field construction).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from bot.config import BoardConfig
from bot.discord_ingest.service import (
    _board_for_channel,
    _triage_relative,
    fetch_triage_items,
    sync_boards,
)
from bot.models import Board, SubmissionLink
from bot.state import SubmissionState

from conftest import make_submission, make_test_settings


def _cfg(name="robots", guild=1, channel=100, nsfw=False) -> BoardConfig:
    return BoardConfig(
        name=name,
        discord_guild_id=guild,
        discord_channel_id=channel,
        nsfw=nsfw,
    )


def _settings_with_boards(*cfgs):
    s = make_test_settings()
    s.boards = list(cfgs)
    return s


# ---------------------------------------------------------------------------
# sync_boards
# ---------------------------------------------------------------------------

async def test_sync_boards_creates_missing_boards(session):
    """Configured boards absent from the DB get a Board row each."""
    settings = _settings_with_boards(
        _cfg(name="robots", channel=100),
        _cfg(name="vehicles", channel=200, nsfw=True),
    )

    await sync_boards(session, settings)

    boards = list(await session.scalars(
        select(Board).order_by(Board.discord_channel_id)
    ))
    assert [(b.name, b.discord_channel_id, b.nsfw) for b in boards] == [
        ("robots", 100, False),
        ("vehicles", 200, True),
    ]


async def test_sync_boards_idempotent(session):
    """Running sync twice does not duplicate rows."""
    settings = _settings_with_boards(
        _cfg(name="robots", channel=100),
        _cfg(name="vehicles", channel=200),
    )

    await sync_boards(session, settings)
    await sync_boards(session, settings)

    count = await session.scalar(select(func.count()).select_from(Board))
    assert count == 2


async def test_sync_boards_updates_existing_row(session, board):
    """Same channel_id with changed config updates name, guild, and nsfw in place."""
    settings = _settings_with_boards(
        _cfg(name="renamed-robots", guild=7, channel=board.discord_channel_id, nsfw=True)
    )

    await sync_boards(session, settings)

    count = await session.scalar(select(func.count()).select_from(Board))
    assert count == 1
    refreshed = await session.get(Board, board.id)
    assert refreshed.name == "renamed-robots"
    assert refreshed.discord_guild_id == 7
    assert refreshed.nsfw is True


# ---------------------------------------------------------------------------
# _board_for_channel
# ---------------------------------------------------------------------------

async def test_board_for_channel_hit(session, board):
    found = await _board_for_channel(session, board.discord_channel_id)
    assert found is not None
    assert found.id == board.id


async def test_board_for_channel_miss(session, board):
    assert await _board_for_channel(session, 424242) is None


# ---------------------------------------------------------------------------
# fetch_triage_items
# ---------------------------------------------------------------------------

async def test_triage_excludes_terminal_states(session, board):
    """Only open submissions come back - published and publish_failed are dropped."""
    open_states = [
        SubmissionState.INTENT_SUBMITTED.value,
        SubmissionState.AWAITING_IMAGE.value,
        SubmissionState.READY_TO_QUEUE.value,
        SubmissionState.QUEUED.value,
    ]
    terminal_states = [
        SubmissionState.PUBLISHED.value,
        SubmissionState.PUBLISH_FAILED.value,
    ]
    for i, state in enumerate(open_states + terminal_states):
        session.add(make_submission(board, state=state, source_discord_message_id=100 + i))
    await session.flush()

    items = await fetch_triage_items(session, board_id=board.id, guild_id=1)

    assert {i.state for i in items} == set(open_states)
    assert len(items) == len(open_states)


async def test_triage_state_filter(session, board):
    """state_filter narrows the result to exactly that open state."""
    session.add(make_submission(
        board, state=SubmissionState.INTENT_SUBMITTED.value, source_discord_message_id=1,
    ))
    session.add(make_submission(
        board, state=SubmissionState.READY_TO_QUEUE.value, source_discord_message_id=2,
    ))
    await session.flush()

    items = await fetch_triage_items(
        session, board_id=board.id, guild_id=1,
        state_filter=SubmissionState.READY_TO_QUEUE.value,
    )

    assert len(items) == 1
    assert items[0].state == SubmissionState.READY_TO_QUEUE.value


async def test_triage_user_filter(session, board):
    """user_id_filter narrows the result to one submitter's open items."""
    session.add(make_submission(
        board, state=SubmissionState.AWAITING_SOURCE.value,
        source_discord_message_id=1, author_id=111, author_display="alice",
    ))
    session.add(make_submission(
        board, state=SubmissionState.AWAITING_SOURCE.value,
        source_discord_message_id=2, author_id=222, author_display="bob",
    ))
    await session.flush()

    items = await fetch_triage_items(session, board_id=board.id, guild_id=1, user_id_filter=111)

    assert len(items) == 1
    assert items[0].author_display == "alice"


async def test_triage_state_and_user_filters_compose(session, board):
    """state_filter and user_id_filter both apply (AND)."""
    session.add(make_submission(
        board, state=SubmissionState.QUEUED.value,
        source_discord_message_id=1, author_id=111, author_display="alice",
    ))
    session.add(make_submission(
        board, state=SubmissionState.AWAITING_SOURCE.value,
        source_discord_message_id=2, author_id=111, author_display="alice",
    ))
    session.add(make_submission(
        board, state=SubmissionState.QUEUED.value,
        source_discord_message_id=3, author_id=222, author_display="bob",
    ))
    await session.flush()

    items = await fetch_triage_items(
        session, board_id=board.id, guild_id=1,
        state_filter=SubmissionState.QUEUED.value, user_id_filter=111,
    )

    assert len(items) == 1
    assert items[0].state == SubmissionState.QUEUED.value
    assert items[0].author_display == "alice"


async def test_triage_ordered_oldest_first(session, board):
    """Items come back ordered by created_at ascending regardless of insert order."""
    now = datetime.now(timezone.utc)
    late = make_submission(
        board, source_discord_message_id=1,
        author_display="late_user", created_at=now,
    )
    early = make_submission(
        board, source_discord_message_id=2,
        author_display="early_user", created_at=now - timedelta(days=1),
    )
    session.add(late)
    session.add(early)
    await session.flush()

    items = await fetch_triage_items(session, board_id=board.id, guild_id=1)

    assert [i.author_display for i in items] == ["early_user", "late_user"]


async def test_triage_thread_url_title_and_author(session, board):
    """thread_url is built from guild_id + thread_id; title comes from the
    order_index 0 link; author_display and submitted_rel are populated."""
    sub = make_submission(
        board, source_discord_message_id=1, thread_id=456,
        author_display="poster", created_at=datetime.now(timezone.utc),
    )
    session.add(sub)
    await session.flush()
    session.add(SubmissionLink(
        submission_id=sub.id, order_index=0,
        raw_url="https://example.com/a", canonical_url="https://example.com/a",
        domain_family="other", resolved_title="Cool Robot",
    ))
    session.add(SubmissionLink(
        submission_id=sub.id, order_index=1,
        raw_url="https://example.com/b", canonical_url="https://example.com/b",
        domain_family="other", resolved_title="Wrong Title",
    ))
    await session.flush()

    items = await fetch_triage_items(session, board_id=board.id, guild_id=77)

    assert len(items) == 1
    item = items[0]
    assert item.thread_url == "https://discord.com/channels/77/456"
    assert item.title == "Cool Robot"
    assert item.author_display == "poster"
    assert item.submitted_rel == "just now"


async def test_triage_fallback_fields(session, board):
    """No thread, no links, no author name - falls back to empty url,
    'submission N' title, and 'unknown' author."""
    sub = make_submission(
        board, source_discord_message_id=1, thread_id=None, author_display="",
    )
    session.add(sub)
    await session.flush()

    items = await fetch_triage_items(session, board_id=board.id, guild_id=1)

    assert len(items) == 1
    item = items[0]
    assert item.thread_url == ""
    assert item.title == f"submission {sub.id}"
    assert item.author_display == "unknown"


def test_triage_relative_buckets():
    """_triage_relative formats None, seconds, minutes, hours, days, and naive dts."""
    now = datetime.now(timezone.utc)
    assert _triage_relative(None) == "?"
    assert _triage_relative(now) == "just now"
    assert _triage_relative(now - timedelta(minutes=5)) == "5m ago"
    assert _triage_relative(now - timedelta(hours=3)) == "3h ago"
    assert _triage_relative(now - timedelta(days=2)) == "2d ago"
    # Naive datetimes are treated as UTC.
    naive = (now - timedelta(hours=1)).replace(tzinfo=None)
    assert _triage_relative(naive) == "1h ago"
