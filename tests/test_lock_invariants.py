"""Invariant tests for docs/db-lock-io-refactor.md.

These pin the design goal: **no network or Discord I/O is ever awaited while the
global SQLite write lock (`bot.db._db_lock`) is held.**

They run against the *real* `session_scope` / real lock (via `global_engine`,
with the lock swapped for an `InstrumentedLock` by the `lock_probe` fixture),
drive a hot path end to end, and assert that the I/O boundaries were not crossed
under the lock.

They are `xfail(strict=True)` today: the current code holds the lock across all
this I/O, so a violation is recorded and the assertion fails (-> xfailed). When a
hot path is refactored to release the lock before doing I/O, its test starts
passing, which - because strict=True - turns the suite RED until the xfail marker
is removed. That is the intended signal that the refactor of that path is done.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

import bot.db as db
from bot.discord_ingest.service import handle_reaction, publish_queued_submission
from bot.models import Board, Submission, SubmissionLink
from bot.state import SubmissionState

from conftest import db_lock_held, make_submission, make_test_settings

pytestmark = pytest.mark.asyncio

QUEUED = SubmissionState.QUEUED.value


def _rec_async(where: str, violations: list[str], return_value: object = None):
    """Build an AsyncMock side-effect that flags calls made under the DB lock."""

    async def _fn(*a, **k):
        if db_lock_held.get():
            violations.append(where)
        return return_value

    return _fn


def _recording_client(violations: list[str]) -> MagicMock:
    """Fake atproto AsyncClient whose I/O methods flag any call made under the
    DB lock. Records rather than raises, so publish's broad except can't hide it.
    """
    client = MagicMock()
    client.me = MagicMock()
    client.me.did = "did:plc:testdid000"

    async def _login(*a, **k):
        if db_lock_held.get():
            violations.append("login")

    create_resp = MagicMock()
    create_resp.uri = "at://did:plc:testdid000/app.bsky.feed.post/abc123"
    create_resp.cid = "bafyreitest000"

    async def _create_record(*a, **k):
        if db_lock_held.get():
            violations.append("create_record")
        return create_resp

    async def _like(*a, **k):
        if db_lock_held.get():
            violations.append("like")

    async def _upload_blob(*a, **k):
        if db_lock_held.get():
            violations.append("upload_blob")
        resp = MagicMock()
        resp.blob = MagicMock()
        return resp

    client.login = AsyncMock(side_effect=_login)
    client.com.atproto.repo.create_record = AsyncMock(side_effect=_create_record)
    client.like = AsyncMock(side_effect=_like)
    client.upload_blob = AsyncMock(side_effect=_upload_blob)
    return client


async def _seed_queued_submission() -> int:
    """Create a board + a QUEUED submission with one external link in the
    global_engine DB. Returns the submission id."""
    async with db.session_scope() as s:
        board = Board(name="robots", discord_guild_id=1, discord_channel_id=100)
        s.add(board)
        await s.flush()
        sub = make_submission(board, state=QUEUED, source_discord_message_id=42)
        s.add(sub)
        await s.flush()
        s.add(SubmissionLink(
            submission_id=sub.id,
            order_index=0,
            raw_url="https://example.com/thing",
            canonical_url="https://example.com/thing",
            domain_family="generic",
            resolved_title="A Thing",
            resolved_description="desc",
        ))
        return sub.id


async def test_publish_does_no_io_under_db_lock(lock_probe):
    # Refactored (docs/db-lock-io-refactor.md): publish_queued_submission is now
    # self-managing - it opens its own short DB scopes and does the network publish
    # with the lock released. Called WITHOUT an outer session_scope, as the
    # scheduler now does; the recording client flags any atproto call under the lock.
    sub_id = await _seed_queued_submission()
    settings = make_test_settings()
    violations: list[str] = []

    with patch("bot.publish.AsyncClient", return_value=_recording_client(violations)):
        await publish_queued_submission(settings, sub_id, None)

    assert violations == [], f"I/O performed under DB lock: {violations}"


# ---------------------------------------------------------------------------
# handle_reaction (the 🦋 path) - the storm trigger
# ---------------------------------------------------------------------------

def _recording_message(violations: list[str], thread: MagicMock, channel_id: int = 100) -> MagicMock:
    """A discord.Message mock carrying a URL, whose channel.create_thread and
    (via the returned thread) sends flag any call made under the DB lock."""
    msg = MagicMock(spec=discord.Message)
    msg.id = 42
    msg.content = "check this out https://example.com/thing"
    msg.embeds = []
    msg.attachments = []
    msg.message_snapshots = []
    msg.created_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
    msg.reference = None
    msg.forward = AsyncMock()

    author = MagicMock()
    author.id = 999
    author.display_name = "testuser"
    msg.author = author

    channel = MagicMock()
    channel.id = channel_id
    channel.create_thread = AsyncMock(side_effect=_rec_async("create_thread", violations, thread))
    msg.channel = channel

    guild = MagicMock()
    guild.id = 1
    guild.get_thread = MagicMock(return_value=None)
    guild.fetch_channel = AsyncMock(side_effect=discord.NotFound(MagicMock(), "not found"))
    msg.guild = guild
    return msg


def _recording_thread(violations: list[str]) -> MagicMock:
    t = MagicMock(spec=discord.Thread)
    t.id = 600
    t.archived = False
    t.guild = MagicMock()
    sent_msg = MagicMock(id=9999, add_reaction=AsyncMock())
    t.send = AsyncMock(side_effect=_rec_async("thread.send", violations, sent_msg))
    t.edit = AsyncMock()
    return t


def _recording_http(violations: list[str]) -> AsyncMock:
    resp = MagicMock()
    resp.status_code = 404
    client = AsyncMock()
    client.get = AsyncMock(side_effect=_rec_async("http.get", violations, resp))
    return client


@pytest.mark.xfail(
    strict=True,
    reason="handle_reaction resolves links and creates the thread under the DB "
           "lock today; flips to pass once refactored per docs/db-lock-io-refactor.md",
)
async def test_handle_reaction_does_no_io_under_db_lock(lock_probe):
    async with db.session_scope() as s:
        s.add(Board(name="robots", discord_guild_id=1, discord_channel_id=100))

    violations: list[str] = []
    thread = _recording_thread(violations)
    msg = _recording_message(violations, thread, channel_id=100)
    http = _recording_http(violations)
    settings = make_test_settings(dashboard_url=None)

    async with db.session_scope() as session:
        await handle_reaction(
            session,
            settings=settings,
            message=msg,
            http_client=http,
            skip_auth=True,
            bot_id=123,
        )

    assert violations == [], f"I/O performed under DB lock: {violations}"

# NOTE: a probabilistic "concurrent recompute double-post" guard was prototyped
# here and deliberately removed - see docs/db-lock-io-refactor.md "Step 2". It
# could not be made to bite: even with the global lock removed AND the two tasks
# forced to interleave past the has_cancel read before either commit, the harness
# never produced a second row (aiosqlite appears to serialize the writes). A test
# that passes whether or not the protection exists is false confidence. The
# per-submission mutual-exclusion property is instead guarded deterministically,
# against the lock itself, as part of the refactor step that introduces it.
