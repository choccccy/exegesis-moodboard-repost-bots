"""Mocked end-to-end flows: the integration seams unit tests can't reach.

Mock boundary: Discord objects are mocks, the DB is real (in-memory SQLite),
outbound HTTP is mocked, and Bluesky is mocked one level deeper than
test_e2e_publish.py - at the atproto AsyncClient constructor - so the entire
publish module (login, dispatch, blob upload, record creation, reply chaining)
runs real code.

Flows covered:
  1. metadata prompt -> curator reply -> confirm button -> QUEUED
  2. queued submission -> scheduler tick -> real publish_submission -> PUBLISHED
  3. button click entering via RepostBot.on_interaction -> QUEUED -> published
  4. multi-video + image submission -> reply chain with threaded parent refs
  5. reply-to submission deferred while parent unpublished, then posts as reply
  6. youtube confirm -> playlist add row via yt_client
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from sqlalchemy import select

from bot.config import BoardConfig
from bot.discord_ingest.service import (
    handle_confirm_button,
    handle_reaction,
    handle_reply,
    publish_queued_submission,
)
from bot.models import (
    Attachment,
    MetadataRequest,
    PublishAttempt,
    Submission,
    SubmissionLink,
    YoutubePlaylistAdd,
)
from bot.scheduler import _fire_board
from bot.state import AltTextStatus, PublishOutcome, SubmissionState

from conftest import make_submission
from test_publish_dispatch import _fake_client, _patched_client, _record_of

_ids = itertools.count(20_000)


def _board_cfg(board, **kw) -> BoardConfig:
    defaults = dict(
        name=board.name,
        discord_guild_id=board.discord_guild_id,
        discord_channel_id=board.discord_channel_id,
        bluesky_handle=f"@{board.name}.exegesis.space",
        require_graphic_classification=False,
        tags=[],
    )
    defaults.update(kw)
    return BoardConfig(**defaults)


def _settings(board, **cfg_kw) -> MagicMock:
    cfg = _board_cfg(board, **cfg_kw)
    s = MagicMock()
    s.board_for_channel.return_value = cfg
    s.bsky_password_for.return_value = "test-app-password"
    s.trigger_emoji = "\N{BUTTERFLY}"
    s.dashboard_url = None
    s.attachments_dir = "/tmp/test-e2e-atts"
    s.data_dir = "/tmp/test-e2e-data"
    s.storage_min_free_mb = 0
    s.youtube_api_key = None
    s.queue_target_days = 90
    s.queue_min_daily = 1
    s.queue_max_daily = 6
    return s


def _thread_mock():
    thread = MagicMock(spec=discord.Thread)
    thread.id = next(_ids)
    thread.archived = False
    thread.edit = AsyncMock()

    async def _send(*args, **kwargs):
        m = MagicMock()
        m.id = next(_ids)
        m.add_reaction = AsyncMock()
        m.edit = AsyncMock()
        return m

    thread.send = _send
    # Status-checklist edit-in-place: get_partial_message(id).edit(...) must be awaitable.
    def _partial(message_id):
        p = MagicMock()
        p.edit = AsyncMock()
        return p
    thread.get_partial_message = _partial
    return thread


def _source_message(board, *, msg_id=None, content="https://example.com/robot", author_id=999):
    thread = _thread_mock()
    msg = MagicMock(spec=discord.Message)
    msg.id = msg_id or next(_ids)
    msg.content = content
    msg.embeds = []
    msg.attachments = []
    msg.message_snapshots = []
    msg.created_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
    msg.reference = None
    author = MagicMock()
    author.id = author_id
    author.display_name = "poster"
    author.mention = "<@999>"
    author.bot = False
    msg.author = author
    msg.guild = MagicMock()
    msg.guild.id = board.discord_guild_id
    msg.channel = MagicMock(spec=discord.TextChannel)
    msg.channel.id = board.discord_channel_id
    msg.create_thread = AsyncMock(return_value=thread)
    msg.channel.create_thread = AsyncMock(return_value=thread)
    msg.add_reaction = AsyncMock()
    msg.forward = AsyncMock()
    msg.remove_reaction = AsyncMock()
    msg.clear_reaction = AsyncMock()
    msg.jump_url = f"https://discord.com/channels/1/100/{msg.id}"
    return msg, thread


def _confirm_interaction(user_id=999):
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = MagicMock()
    interaction.user.id = user_id
    interaction.user.display_name = "poster"
    member = MagicMock()
    member.roles = []
    interaction.user.roles = []
    interaction.channel = _thread_mock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    interaction.message = MagicMock()
    interaction.message.edit = AsyncMock()
    return interaction


async def _ingest(session, settings, msg, resolve_side_effect=None):
    """Run handle_reaction with link resolution stubbed."""
    resolver = AsyncMock(side_effect=resolve_side_effect)
    with patch("bot.discord_ingest.service._resolve_links", resolver):
        await handle_reaction(
            session, settings=settings, message=msg,
            http_client=AsyncMock(), skip_auth=True,
        )
    return await session.scalar(
        select(Submission).where(Submission.source_discord_message_id == msg.id)
    )


def _write_resolved(link, *, title="Cool Robot", via="opengraph"):
    link.resolved_title = title
    link.resolved_description = "desc"
    link.resolved_via = via
    link.resolved_image_url = "https://cdn.example.com/thumb.jpg"
    link.resolved_image_path = "/tmp/test-e2e-atts/thumb.jpg"


# ---------------------------------------------------------------------------
# Flow 1: metadata prompt -> curator reply -> confirm -> QUEUED
# ---------------------------------------------------------------------------


async def test_e2e_prompt_answer_metadata_to_queue(session, board):
    settings = _settings(board)
    msg, thread = _source_message(board, content="https://example.com/unresolvable")

    # Ingest with resolution finding nothing: metadata gap -> prompt in thread.
    sub = await _ingest(session, settings, msg)
    assert sub is not None
    assert sub.state == SubmissionState.AWAITING_BETTER_LINK.value

    meta_req = await session.scalar(
        select(MetadataRequest).where(
            MetadataRequest.submission_id == sub.id,
            MetadataRequest.answered_at.is_(None),
        )
    )
    assert meta_req is not None, "a metadata request should be open in the thread"

    # Curator replies to the prompt with a better link; this resolve succeeds.
    async def _resolve_ok(session_, submission, settings_, http_client, **kw):
        links = (await session_.scalars(
            select(SubmissionLink).where(SubmissionLink.submission_id == submission.id)
        )).all()
        for link in links:
            _write_resolved(link)

    reply = MagicMock(spec=discord.Message)
    reply.id = next(_ids)
    reply.content = "https://example.com/better-link"
    reply.attachments = []
    reply.embeds = []
    reply.author = msg.author
    reply.channel = thread
    reply.reference = MagicMock()
    reply.reference.message_id = meta_req.bot_message_id
    reply.add_reaction = AsyncMock()
    reply.reply = AsyncMock()

    with patch("bot.discord_ingest.service._resolve_links", AsyncMock(side_effect=_resolve_ok)):
        handled = await handle_reply(
            session, settings=settings, message=reply, http_client=AsyncMock(),
        )
    assert handled is True

    await session.flush()
    assert sub.state == SubmissionState.READY_TO_QUEUE.value

    # Confirm button queues it.
    await handle_confirm_button(session, _confirm_interaction(), sub.id, settings)
    await session.flush()
    assert sub.state == SubmissionState.QUEUED.value


# ---------------------------------------------------------------------------
# Flow 2: queued -> scheduler tick -> REAL publish module -> PUBLISHED
# ---------------------------------------------------------------------------


async def test_e2e_scheduler_tick_publishes_through_real_publish_module(session, board):
    settings = _settings(board)
    sub = make_submission(board, state=SubmissionState.QUEUED.value,
                          source_discord_message_id=next(_ids))
    session.add(sub)
    await session.flush()
    link = SubmissionLink(
        submission_id=sub.id, order_index=0,
        raw_url="https://example.com/robot", canonical_url="https://example.com/robot",
        domain_family="other",
    )
    _write_resolved(link)
    session.add(link)
    await session.flush()

    board_cfg = MagicMock()
    board_cfg.name = board.name
    board_cfg.discord_channel_id = board.discord_channel_id
    board_cfg.bluesky_handle = f"@{board.name}.exegesis.space"

    fake = _fake_client()
    now = datetime.now(timezone.utc)
    bot = MagicMock()
    bot.fetch_channel = AsyncMock(side_effect=Exception("no thread"))
    with _patched_client(fake):
        await _fire_board(session, bot, settings, board_cfg,
                          now - timedelta(hours=72), now - timedelta(hours=1))

    await session.flush()
    assert sub.state == SubmissionState.PUBLISHED.value
    fake.login.assert_awaited_once()
    attempt = await session.scalar(
        select(PublishAttempt).where(PublishAttempt.submission_id == sub.id)
    )
    assert attempt is not None and attempt.success is True and attempt.error is None
    assert attempt.at_uri and attempt.at_uri.startswith("at://")
    record = _record_of(fake.com.atproto.repo.create_record.call_args)
    assert "https://example.com/robot" in record.text


# ---------------------------------------------------------------------------
# Flow 3: RepostBot.on_interaction -> QUEUED -> published (client->service seam)
# ---------------------------------------------------------------------------


async def test_e2e_button_confirm_full_cycle_via_client(session, board, repost_bot):
    from conftest import bound_session_scope, make_interaction

    settings = _settings(board)
    repost_bot.settings = settings
    sub = make_submission(board, state=SubmissionState.READY_TO_QUEUE.value,
                          source_discord_message_id=next(_ids))
    session.add(sub)
    await session.flush()
    link = SubmissionLink(
        submission_id=sub.id, order_index=0,
        raw_url="https://example.com/x", canonical_url="https://example.com/x",
        domain_family="other",
    )
    _write_resolved(link)
    session.add(link)
    # An open confirmation request, as recompute would have left it.
    from bot.models import ConfirmationRequest
    session.add(ConfirmationRequest(
        submission_id=sub.id, bot_message_id=next(_ids),
    ))
    await session.flush()

    interaction = make_interaction(custom_id=f"confirm:{sub.id}", user_id=999)
    interaction.user.display_name = "poster"
    interaction.user.roles = []
    interaction.message = MagicMock()
    interaction.message.edit = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()

    with patch("bot.discord_ingest.client.session_scope", bound_session_scope(session)):
        await repost_bot.on_interaction(interaction)

    await session.flush()
    assert sub.state == SubmissionState.QUEUED.value, "confirm click must queue via on_interaction routing"

    # And the queued submission publishes for real on the next tick.
    fake = _fake_client()
    with _patched_client(fake):
        outcome = await publish_queued_submission(session, settings, sub, None)
    assert outcome is PublishOutcome.PUBLISHED
    await session.flush()
    assert sub.state == SubmissionState.PUBLISHED.value


# ---------------------------------------------------------------------------
# Flow 4: multi-video + image -> reply chain with threaded refs
# ---------------------------------------------------------------------------


async def test_e2e_reply_chain_multi_video_and_images(session, board, tmp_path):
    settings = _settings(board)
    sub = make_submission(board, state=SubmissionState.QUEUED.value,
                          source_discord_message_id=next(_ids))
    session.add(sub)
    await session.flush()
    link = SubmissionLink(
        submission_id=sub.id, order_index=0,
        raw_url="https://example.com/vids", canonical_url="https://example.com/vids",
        domain_family="other",
    )
    _write_resolved(link)
    session.add(link)

    for i, name in enumerate(["a.mp4", "b.mp4"]):
        f = tmp_path / name
        f.write_bytes(b"fake-video-bytes")
        session.add(Attachment(
            submission_id=sub.id, discord_attachment_id=next(_ids),
            filename=name, discord_url=f"https://cdn.discord.com/{name}",
            mime="video/mp4", is_image=False, is_video=True,
            alt_text_status=AltTextStatus.PROVIDED.value, alt_text_body=f"video {i}",
            local_path=str(f),
        ))
    img = tmp_path / "pic.jpg"
    img.write_bytes(b"fake-image-bytes")
    session.add(Attachment(
        submission_id=sub.id, discord_attachment_id=next(_ids),
        filename="pic.jpg", discord_url="https://cdn.discord.com/pic.jpg",
        mime="image/jpeg", is_image=True, is_video=False,
        alt_text_status=AltTextStatus.PROVIDED.value, alt_text_body="a pic",
        local_path=str(img),
    ))
    await session.flush()

    fake = _fake_client()
    # Unique rkey per created record so parent/root refs are distinguishable.
    counter = itertools.count()

    async def _create(data):
        resp = MagicMock()
        resp.uri = f"at://did:plc:testdid000/app.bsky.feed.post/rk{next(counter)}"
        resp.cid = f"cid{next(counter)}"
        return resp

    fake.com.atproto.repo.create_record = AsyncMock(side_effect=_create)

    with _patched_client(fake):
        outcome = await publish_queued_submission(session, settings, sub, None)

    assert outcome is PublishOutcome.PUBLISHED
    calls = fake.com.atproto.repo.create_record.call_args_list
    assert len(calls) == 3, "main video post + extra video reply + image reply"

    main_record = _record_of(calls[0])
    video_reply = _record_of(calls[1])
    image_reply = _record_of(calls[2])

    assert main_record.reply is None
    # counter yields uri rk0/cid1 for the main post, rk2/cid3 for the video
    # reply, rk4/cid5 for the image reply.
    root_uri = "at://did:plc:testdid000/app.bsky.feed.post/rk0"
    video_reply_uri = "at://did:plc:testdid000/app.bsky.feed.post/rk2"
    assert video_reply.reply.root.uri == root_uri
    assert video_reply.reply.parent.uri == root_uri
    assert image_reply.reply.root.uri == root_uri
    assert image_reply.reply.parent.uri == video_reply_uri, \
        "image reply must chain after the video reply, not the root"


# ---------------------------------------------------------------------------
# Flow 5: reply-to deferral, then posts as a Bluesky reply
# ---------------------------------------------------------------------------


async def test_e2e_parent_reply_deferral_then_reply_publish(session, board):
    settings = _settings(board)
    parent_msg_id = next(_ids)

    parent = make_submission(board, state=SubmissionState.QUEUED.value,
                             source_discord_message_id=parent_msg_id,
                             created_at=datetime(2026, 1, 2, tzinfo=timezone.utc))
    child = make_submission(board, state=SubmissionState.QUEUED.value,
                            source_discord_message_id=next(_ids),
                            reply_to_discord_message_id=parent_msg_id,
                            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    session.add_all([parent, child])
    await session.flush()
    for s, url in ((parent, "https://example.com/parent"), (child, "https://example.com/child")):
        link = SubmissionLink(submission_id=s.id, order_index=0,
                              raw_url=url, canonical_url=url, domain_family="other")
        _write_resolved(link)
        session.add(link)
    await session.flush()

    board_cfg = MagicMock()
    board_cfg.name = board.name
    board_cfg.discord_channel_id = board.discord_channel_id
    board_cfg.bluesky_handle = f"@{board.name}.exegesis.space"

    fake = _fake_client()
    now = datetime.now(timezone.utc)
    bot = MagicMock()
    bot.fetch_channel = AsyncMock(side_effect=Exception("no thread"))

    # Tick 1: child is older so it's picked first, defers (parent unpublished),
    # scheduler falls through and publishes the parent instead.
    with _patched_client(fake):
        await _fire_board(session, bot, settings, board_cfg,
                          now - timedelta(hours=72), now - timedelta(hours=1))
    await session.flush()
    assert parent.state == SubmissionState.PUBLISHED.value
    assert child.state == SubmissionState.QUEUED.value

    parent_attempt = await session.scalar(
        select(PublishAttempt).where(PublishAttempt.submission_id == parent.id)
    )

    # Tick 2 (cap allows another): child now publishes as a reply to the parent.
    fake2 = _fake_client()
    with _patched_client(fake2):
        outcome = await publish_queued_submission(session, settings, child, None)
    assert outcome is PublishOutcome.PUBLISHED
    child_record = _record_of(fake2.com.atproto.repo.create_record.call_args)
    assert child_record.reply is not None
    assert child_record.reply.parent.uri == parent_attempt.at_uri


# ---------------------------------------------------------------------------
# Flow 6: youtube confirm -> playlist add through yt_client
# ---------------------------------------------------------------------------


async def test_e2e_confirm_triggers_playlist_add(session, board):
    settings = _settings(board, youtube_playlist_id="PLtest123")
    sub = make_submission(board, state=SubmissionState.READY_TO_QUEUE.value,
                          source_discord_message_id=next(_ids))
    session.add(sub)
    await session.flush()
    link = SubmissionLink(
        submission_id=sub.id, order_index=0,
        raw_url="https://www.youtube.com/watch?v=vid123",
        canonical_url="https://www.youtube.com/watch?v=vid123",
        domain_family="youtube",
    )
    _write_resolved(link, title="A Video")
    session.add(link)
    from bot.models import ConfirmationRequest
    session.add(ConfirmationRequest(
        submission_id=sub.id, bot_message_id=next(_ids),
    ))
    await session.flush()

    yt_client = MagicMock()
    yt_client.add_to_playlist.return_value = "playlist-item-1"

    await handle_confirm_button(session, _confirm_interaction(), sub.id, settings,
                                yt_client=yt_client)

    await session.flush()
    assert sub.state == SubmissionState.QUEUED.value
    row = await session.scalar(
        select(YoutubePlaylistAdd).where(
            YoutubePlaylistAdd.source_discord_message_id == sub.source_discord_message_id
        )
    )
    assert row is not None and row.success is True
    assert row.video_id == "vid123"
    yt_client.add_to_playlist.assert_called_once_with("PLtest123", "vid123")
