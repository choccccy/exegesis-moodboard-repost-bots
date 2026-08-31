"""Tests for _find_prior_post and _find_duplicate helper functions."""
from __future__ import annotations

from bot.curation.handlers import _find_prior_post, _find_duplicate
from bot.models import PublishAttempt, SubmissionLink, SubmissionThread
from bot.state import SubmissionState

from conftest import make_submission


async def _add_link(session, submission, url: str, order_index: int = 0):
    link = SubmissionLink(
        submission_id=submission.id,
        order_index=order_index,
        raw_url=url,
        canonical_url=url,
        domain_family="example",
    )
    session.add(link)
    await session.flush()
    return link


async def _add_attempt(session, submission, *, success: bool, bsky_url: str | None = None, at_uri: str | None = None):
    attempt = PublishAttempt(
        submission_id=submission.id,
        success=success,
        bsky_url=bsky_url,
        at_uri=at_uri,
    )
    session.add(attempt)
    await session.flush()
    return attempt


# --- _find_prior_post --------------------------------------------------------


async def test_find_prior_post_returns_none_when_no_match(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()

    result = await _find_prior_post(session, "https://example.com/foo", sub.id)
    assert result is None


async def test_find_prior_post_returns_bsky_url_when_published(session, board):
    sub = make_submission(board, state=SubmissionState.PUBLISHED.value)
    session.add(sub)
    await session.flush()
    await _add_link(session, sub, "https://example.com/foo")
    await _add_attempt(session, sub, success=True, bsky_url="https://bsky.app/profile/x/post/abc")

    new_sub = make_submission(board, source_discord_message_id=2)
    session.add(new_sub)
    await session.flush()

    result = await _find_prior_post(session, "https://example.com/foo", new_sub.id)
    assert result == "https://bsky.app/profile/x/post/abc"


async def test_find_prior_post_falls_back_to_at_uri(session, board):
    sub = make_submission(board, state=SubmissionState.PUBLISHED.value)
    session.add(sub)
    await session.flush()
    await _add_link(session, sub, "https://example.com/bar")
    await _add_attempt(session, sub, success=True, bsky_url=None, at_uri="at://did:plc:xxx/app.bsky.feed.post/yyy")

    new_sub = make_submission(board, source_discord_message_id=2)
    session.add(new_sub)
    await session.flush()

    result = await _find_prior_post(session, "https://example.com/bar", new_sub.id)
    assert result == "at://did:plc:xxx/app.bsky.feed.post/yyy"


async def test_find_prior_post_excludes_same_submission(session, board):
    sub = make_submission(board, state=SubmissionState.PUBLISHED.value)
    session.add(sub)
    await session.flush()
    await _add_link(session, sub, "https://example.com/baz")
    await _add_attempt(session, sub, success=True, bsky_url="https://bsky.app/x")

    # exclude_submission_id == sub.id, so it should return None
    result = await _find_prior_post(session, "https://example.com/baz", sub.id)
    assert result is None


async def test_find_prior_post_ignores_failed_attempts(session, board):
    sub = make_submission(board, state=SubmissionState.PUBLISH_FAILED.value)
    session.add(sub)
    await session.flush()
    await _add_link(session, sub, "https://example.com/fail")
    await _add_attempt(session, sub, success=False, bsky_url=None)

    new_sub = make_submission(board, source_discord_message_id=2)
    session.add(new_sub)
    await session.flush()

    result = await _find_prior_post(session, "https://example.com/fail", new_sub.id)
    assert result is None


# --- _find_duplicate ---------------------------------------------------------


async def test_find_duplicate_returns_none_when_no_match(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()

    result = await _find_duplicate(session, "https://example.com/unique", sub.id, guild_id=1)
    assert result is None


async def test_find_duplicate_published_takes_priority(session, board):
    published = make_submission(board, state=SubmissionState.PUBLISHED.value)
    session.add(published)
    await session.flush()
    await _add_link(session, published, "https://example.com/post")
    await _add_attempt(session, published, success=True, bsky_url="https://bsky.app/x/y")

    new_sub = make_submission(board, source_discord_message_id=2)
    session.add(new_sub)
    await session.flush()

    result = await _find_duplicate(session, "https://example.com/post", new_sub.id, guild_id=1)
    assert result is not None
    kind, url, _thread = result
    assert kind == "published"
    assert url == "https://bsky.app/x/y"


async def test_find_duplicate_published_carries_old_thread(session, board):
    # #63: a published duplicate links to the old post *and* its curation thread.
    published = make_submission(
        board, state=SubmissionState.PUBLISHED.value, thread_id=321, status_message_id=654,
    )
    session.add(published)
    await session.flush()
    await _add_link(session, published, "https://example.com/pubthread")
    await _add_attempt(session, published, success=True, bsky_url="https://bsky.app/x/z")

    new_sub = make_submission(board, source_discord_message_id=2)
    session.add(new_sub)
    await session.flush()

    result = await _find_duplicate(session, "https://example.com/pubthread", new_sub.id, guild_id=5)
    assert result == ("published", "https://bsky.app/x/z", "https://discord.com/channels/5/321/654")


async def test_find_duplicate_active_pending(session, board):
    pending = make_submission(board, state=SubmissionState.READY_TO_QUEUE.value)
    pending.thread_id = 777
    session.add(pending)
    await session.flush()
    await _add_link(session, pending, "https://example.com/pending")

    new_sub = make_submission(board, source_discord_message_id=2)
    session.add(new_sub)
    await session.flush()

    result = await _find_duplicate(session, "https://example.com/pending", new_sub.id, guild_id=1)
    assert result is not None
    kind, thread_url, _ = result
    assert kind == "pending"
    assert thread_url is not None and "777" in thread_url


async def test_find_duplicate_active_queued(session, board):
    queued = make_submission(board, state=SubmissionState.QUEUED.value)
    queued.thread_id = 888
    session.add(queued)
    await session.flush()
    await _add_link(session, queued, "https://example.com/queued")

    new_sub = make_submission(board, source_discord_message_id=2)
    session.add(new_sub)
    await session.flush()

    result = await _find_duplicate(session, "https://example.com/queued", new_sub.id, guild_id=1)
    assert result is not None
    kind, _url, _ = result
    assert kind == "queued"


async def test_find_duplicate_excludes_self(session, board):
    sub = make_submission(board, state=SubmissionState.READY_TO_QUEUE.value)
    session.add(sub)
    await session.flush()
    await _add_link(session, sub, "https://example.com/self")

    result = await _find_duplicate(session, "https://example.com/self", sub.id, guild_id=1)
    assert result is None


# --- #63: the "already queued" link is deep + resilient ----------------------


async def test_find_duplicate_deep_links_to_status_message(session, board):
    queued = make_submission(
        board, state=SubmissionState.QUEUED.value, thread_id=888, status_message_id=999,
    )
    session.add(queued)
    await session.flush()
    await _add_link(session, queued, "https://example.com/deep")

    new_sub = make_submission(board, source_discord_message_id=2)
    session.add(new_sub)
    await session.flush()

    result = await _find_duplicate(session, "https://example.com/deep", new_sub.id, guild_id=5)
    assert result == ("queued", "https://discord.com/channels/5/888/999", None)


async def test_find_duplicate_falls_back_to_submission_thread_mapping(session, board):
    # thread_id was cleared on the submission (e.g. teardown), but the durable
    # SubmissionThread mapping still knows where the thread lives.
    queued = make_submission(
        board, state=SubmissionState.QUEUED.value, source_discord_message_id=42, thread_id=None,
    )
    session.add(queued)
    await session.flush()
    await _add_link(session, queued, "https://example.com/durable")
    session.add(SubmissionThread(
        board_id=board.id, source_discord_message_id=42, thread_id=1234,
    ))
    await session.flush()

    new_sub = make_submission(board, source_discord_message_id=2)
    session.add(new_sub)
    await session.flush()

    result = await _find_duplicate(session, "https://example.com/durable", new_sub.id, guild_id=5)
    assert result == ("queued", "https://discord.com/channels/5/1234", None)


async def test_find_duplicate_no_link_without_guild(session, board):
    # guild_id 0 (DM / unknown guild) -> no jump link can be built.
    queued = make_submission(
        board, state=SubmissionState.QUEUED.value, thread_id=888, status_message_id=999,
    )
    session.add(queued)
    await session.flush()
    await _add_link(session, queued, "https://example.com/noguild")

    new_sub = make_submission(board, source_discord_message_id=2)
    session.add(new_sub)
    await session.flush()

    result = await _find_duplicate(session, "https://example.com/noguild", new_sub.id, guild_id=0)
    assert result == ("queued", None, None)


async def test_find_duplicate_no_link_without_thread(session, board):
    # No live thread_id and no durable mapping -> reply degrades to linkless text.
    queued = make_submission(
        board, state=SubmissionState.QUEUED.value, source_discord_message_id=77, thread_id=None,
    )
    session.add(queued)
    await session.flush()
    await _add_link(session, queued, "https://example.com/nothread")

    new_sub = make_submission(board, source_discord_message_id=2)
    session.add(new_sub)
    await session.flush()

    result = await _find_duplicate(session, "https://example.com/nothread", new_sub.id, guild_id=5)
    assert result == ("queued", None, None)
