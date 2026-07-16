"""Last-mile branch sweep for bot.discord_ingest.service.

Targets the remaining uncovered statements and branches: mostly two-line
except-swallow log paths (Discord sends that fail), early-return guards
(missing rows, unauthorized users, unresolvable threads), and a few odd
input shapes (snapshot embeds, null canonical URLs, naive datetimes).
Complements test_handle_reaction.py, test_cancel_flows.py,
test_attempt_publish.py and the recompute/e2e suites.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from sqlalchemy import select

from bot.asset_store import StorageFullError
from bot.config import BoardConfig
from bot.discord_ingest import replies
from bot.discord_ingest.service import (
    _apply_answer,
    _attempt_publish,
    _build_post_preview,
    _curator_authorized,
    _determine_kind,
    _discord_file_for_attachment,
    _ingest_attachment,
    _ingest_content,
    _ingest_resolved_video,
    _is_authorized,
    _post_thread_anchor,
    _resolve_links,
    _resolve_parent_ref,
    _resolve_thread,
    handle_cancel_button,
    handle_cancel_reaction,
    handle_confirm_button,
    handle_confirmation_reaction,
    handle_label_reaction,
    handle_metadata_confirm_button,
    handle_metadata_reaction,
    handle_playlist_opt_out,
    handle_playlist_skip_button,
    handle_reaction,
    handle_reaction_removed,
    publish_queued_submission,
    recompute_and_request,
)
from bot.ingest.types import InboundAttachment, InboundEmbed, InboundMessage, InboundSnapshot
from bot.models import (
    Attachment,
    AttachmentAltTextRequest,
    CancellationRequest,
    ConfirmationRequest,
    ContentLabelRequest,
    MetadataRequest,
    PublishAttempt,
    Submission,
    SubmissionLink,
)
from bot.publish import PublishResult
from bot.resolve import ResolvedMetadata
from bot.state import AltTextStatus, PublishOutcome, SubmissionState

from conftest import MockDest, make_interaction, make_submission

QUEUED = SubmissionState.QUEUED.value
PUBLISHED = SubmissionState.PUBLISHED.value


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _forbidden() -> discord.Forbidden:
    return discord.Forbidden(MagicMock(status=403), "forbidden")


def _svc_settings(
    board,
    *,
    curator_user_ids=(),
    youtube_playlist_id=None,
    bluesky_handle="robots.exegesis.space",
    require_graphic=False,
    password="app-password",
    tmp_dir="/tmp/attachments",
):
    """MagicMock Settings with a real BoardConfig for the test board."""
    cfg = BoardConfig(
        name=board.name,
        discord_guild_id=board.discord_guild_id,
        discord_channel_id=board.discord_channel_id,
        bluesky_handle=bluesky_handle,
        curator_user_ids=list(curator_user_ids),
        youtube_playlist_id=youtube_playlist_id,
        require_graphic_classification=require_graphic,
    )
    s = MagicMock()
    s.board_for_channel.return_value = cfg
    s.bsky_password_for.return_value = password
    s.attachments_dir = tmp_dir
    s.data_dir = tmp_dir
    s.storage_min_free_mb = 1
    s.youtube_api_key = None
    s.trigger_emoji = "\N{BUTTERFLY}"
    s.dashboard_url = None
    return s


def _message(channel_id: int, msg_id: int = 42, author_id: int = 999) -> MagicMock:
    msg = MagicMock(spec=discord.Message)
    msg.id = msg_id
    msg.content = ""
    msg.embeds = []
    msg.attachments = []
    msg.message_snapshots = []
    msg.created_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
    author = MagicMock()
    author.id = author_id
    author.display_name = "testuser"
    msg.author = author
    channel = MagicMock()
    channel.id = channel_id
    channel.create_thread = AsyncMock()
    msg.channel = channel
    guild = MagicMock()
    guild.id = 1
    guild.get_thread = MagicMock(return_value=None)
    guild.fetch_channel = AsyncMock(side_effect=discord.NotFound(MagicMock(), "not found"))
    msg.guild = guild
    msg.forward = AsyncMock()
    msg.reference = None
    return msg


def _thread(thread_id: int = 500) -> MagicMock:
    t = MagicMock(spec=discord.Thread)
    t.id = thread_id
    t.archived = False
    t.send = AsyncMock(return_value=MagicMock(id=9999, add_reaction=AsyncMock()))
    t.edit = AsyncMock()
    t.guild = MagicMock()
    return t


class RaisingDest:
    """Notifier whose send() always raises discord.Forbidden; archive records."""

    def __init__(self):
        self.archived: list[str] = []

    async def send(self, content=None, **kwargs):
        raise discord.Forbidden(MagicMock(status=403), "forbidden")

    async def archive(self, notice: str) -> None:
        self.archived.append(notice)


async def _add_link(session, submission_id, url, **kw):
    defaults = dict(
        submission_id=submission_id,
        order_index=0,
        raw_url=url,
        canonical_url=url,
        domain_family="other",
    )
    defaults.update(kw)
    link = SubmissionLink(**defaults)
    session.add(link)
    await session.flush()
    return link


# ---------------------------------------------------------------------------
# handle_reaction early exits + published-duplicate notice
# ---------------------------------------------------------------------------


async def test_handle_reaction_ignores_unwatched_channel(session, board):
    msg = _message(channel_id=555_555)  # no Board row for this channel
    result = await handle_reaction(
        session, settings=MagicMock(), message=msg, http_client=AsyncMock(), skip_auth=True
    )
    assert result is False
    msg.channel.create_thread.assert_not_called()


async def test_handle_reaction_rejects_non_curator(session, board):
    msg = _message(channel_id=board.discord_channel_id)
    settings = _svc_settings(board)  # empty curator lists
    result = await handle_reaction(
        session, settings=settings, message=msg, http_client=AsyncMock(),
        member=None, user_id=12345, skip_auth=False,
    )
    assert result is False
    sub = await session.scalar(select(Submission))
    assert sub is None


async def test_handle_reaction_duplicate_of_published_closes_thread(session, board):
    dup_url = "https://example.com/already-posted"
    prior = make_submission(board, state=PUBLISHED, source_discord_message_id=777)
    session.add(prior)
    await session.flush()
    await _add_link(session, prior.id, dup_url)
    session.add(PublishAttempt(
        submission_id=prior.id, success=True,
        at_uri="at://did/x", at_cid="c", bsky_url="https://bsky.app/profile/x/post/old",
    ))
    await session.flush()

    msg = _message(channel_id=board.discord_channel_id, msg_id=4242)
    msg.content = dup_url
    new_thread = _thread(thread_id=800)
    msg.channel.create_thread.return_value = new_thread

    with patch("bot.discord_ingest.service._resolve_links", new_callable=AsyncMock), \
         patch("bot.discord_ingest.service.remove_submission_dir"), \
         patch("bot.discord_ingest.service._clear_trigger_reaction", new_callable=AsyncMock), \
         patch("bot.discord_ingest.service._archive_thread", new_callable=AsyncMock) as mock_archive:
        result = await handle_reaction(
            session, settings=_svc_settings(board), message=msg,
            http_client=AsyncMock(), skip_auth=True,
        )

    assert result is False
    texts = [c.args[0] if c.args else "" for c in new_thread.send.call_args_list]
    assert any("already been posted" in t and "bsky.app" in t for t in texts)
    mock_archive.assert_called_once()
    remaining = await session.scalar(
        select(Submission).where(Submission.source_discord_message_id == 4242)
    )
    assert remaining is None


# ---------------------------------------------------------------------------
# _post_thread_anchor failure tolerance + playlist opt-out prompt
# ---------------------------------------------------------------------------


async def test_post_thread_anchor_survives_send_and_forward_failures(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    msg = _message(channel_id=board.discord_channel_id)
    msg.guild = None  # jump-link fallback uses guild_id 0
    msg.forward = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=400), "nope"))
    thread = _thread()
    thread.send = AsyncMock(side_effect=_forbidden())

    # Must not raise despite every Discord call failing.
    await _post_thread_anchor(_svc_settings(board), msg, sub, thread)

    assert thread.send.call_count == 2  # anchor + jump-link fallback both attempted


async def test_post_thread_anchor_posts_playlist_opt_out(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    msg = _message(channel_id=board.discord_channel_id)
    thread = _thread()
    thread.send = AsyncMock(return_value=MagicMock(id=4321))
    settings = _svc_settings(board, youtube_playlist_id="PL9")

    await _post_thread_anchor(settings, msg, sub, thread)

    assert sub.playlist_opt_out_message_id == 4321


async def test_post_thread_anchor_playlist_opt_out_send_failure_swallowed(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    msg = _message(channel_id=board.discord_channel_id)
    thread = _thread()
    thread.send = AsyncMock(side_effect=[MagicMock(id=1), _forbidden()])
    settings = _svc_settings(board, youtube_playlist_id="PL9")

    await _post_thread_anchor(settings, msg, sub, thread)

    assert sub.playlist_opt_out_message_id is None


# ---------------------------------------------------------------------------
# _resolve_thread edge cases
# ---------------------------------------------------------------------------


async def test_resolve_thread_edge_cases():
    no_guild = MagicMock(spec=discord.Message)
    no_guild.guild = None
    assert await _resolve_thread(no_guild, 1) is None

    msg = MagicMock(spec=discord.Message)
    msg.guild = MagicMock()
    msg.guild.get_thread.return_value = None
    msg.guild.fetch_channel = AsyncMock(side_effect=discord.NotFound(MagicMock(), "gone"))
    assert await _resolve_thread(msg, 2) is None

    msg.guild.fetch_channel = AsyncMock(return_value=MagicMock())  # not a Thread
    assert await _resolve_thread(msg, 3) is None

    real = MagicMock(spec=discord.Thread)
    msg.guild.fetch_channel = AsyncMock(return_value=real)
    assert await _resolve_thread(msg, 4) is real


# ---------------------------------------------------------------------------
# handle_reaction_removed: published-submission guard variants
# ---------------------------------------------------------------------------


async def test_reaction_removed_published_without_thread_noop(session, board):
    sub = make_submission(board, state=PUBLISHED, source_discord_message_id=61)
    session.add(sub)
    await session.flush()
    settings = _svc_settings(board, curator_user_ids=[42])

    await handle_reaction_removed(
        session, settings=settings, channel=MagicMock(),
        channel_id=board.discord_channel_id, message_id=61, user_id=42,
    )

    still = await session.scalar(select(Submission).where(Submission.id == sub.id))
    assert still is not None  # published submissions are never deleted


async def test_reaction_removed_published_at_uri_fallback(session, board):
    sub = make_submission(board, state=PUBLISHED, source_discord_message_id=62)
    sub.thread_id = 500
    session.add(sub)
    await session.flush()
    session.add(PublishAttempt(
        submission_id=sub.id, success=True,
        at_uri="at://did:plc:z/app.bsky.feed.post/rr", at_cid="c", bsky_url=None,
    ))
    thread = _thread(500)
    channel = MagicMock()
    channel.guild.get_thread.return_value = thread
    settings = _svc_settings(board, curator_user_ids=[42])

    await handle_reaction_removed(
        session, settings=settings, channel=channel,
        channel_id=board.discord_channel_id, message_id=62, user_id=42,
    )

    sent = thread.send.call_args.args[0]
    assert "bsky.app/profile/robots.exegesis.space/post/rr" in sent


async def test_reaction_removed_published_no_attempt_generic_name(session, board):
    sub = make_submission(board, state=PUBLISHED, source_discord_message_id=63)
    sub.thread_id = 501
    session.add(sub)
    await session.flush()
    thread = _thread(501)
    channel = MagicMock()
    channel.guild.get_thread.return_value = thread
    settings = _svc_settings(board, curator_user_ids=[42])

    await handle_reaction_removed(
        session, settings=settings, channel=channel,
        channel_id=board.discord_channel_id, message_id=63, user_id=42,
    )

    assert thread.send.call_args.args[0] == replies.cannot_remove_published("Bluesky")


# ---------------------------------------------------------------------------
# label / metadata / confirmation reaction guards
# ---------------------------------------------------------------------------


async def test_label_reaction_missing_submission(session, board):
    session.add(ContentLabelRequest(submission_id=999_999, bot_message_id=901))
    await session.flush()
    channel = MagicMock()
    channel.send = AsyncMock()

    await handle_label_reaction(
        session, settings=MagicMock(), channel=channel, message_id=901,
        emoji="\N{DROP OF BLOOD}", member=None, user_id=1,
    )

    channel.send.assert_not_called()


async def test_label_reaction_unauthorized(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    session.add(ContentLabelRequest(submission_id=sub.id, bot_message_id=902))
    await session.flush()
    settings = MagicMock()
    settings.board_for_channel.return_value = None

    await handle_label_reaction(
        session, settings=settings, channel=MagicMock(), message_id=902,
        emoji="\N{DROP OF BLOOD}", member=None, user_id=55,  # not OP, no curators
    )

    assert sub.graphic_status == "unknown"


async def test_metadata_reaction_no_open_request(session, board):
    channel = MagicMock()
    channel.send = AsyncMock()
    await handle_metadata_reaction(
        session, settings=MagicMock(), channel=channel, message_id=54321,
        member=None, user_id=1,
    )
    channel.send.assert_not_called()


async def test_metadata_reaction_missing_submission(session, board):
    session.add(MetadataRequest(submission_id=999_999, bot_message_id=903))
    await session.flush()
    channel = MagicMock()
    channel.send = AsyncMock()

    await handle_metadata_reaction(
        session, settings=MagicMock(), channel=channel, message_id=903,
        member=None, user_id=1,
    )

    channel.send.assert_not_called()


async def test_confirmation_reaction_terminal_state_returns_false(session, board):
    sub = make_submission(board, state=QUEUED)
    session.add(sub)
    await session.flush()
    session.add(ConfirmationRequest(submission_id=sub.id, bot_message_id=904))
    await session.flush()

    result = await handle_confirmation_reaction(
        session, settings=MagicMock(), channel=MagicMock(), message_id=904,
        member=None, user_id=999,
    )

    assert result is False
    assert sub.state == QUEUED


async def test_confirmation_reaction_playlist_skipped_archives_thread(session, board):
    sub = make_submission(board, state=SubmissionState.READY_TO_QUEUE.value, source_waived=True)
    sub.playlist_skipped = True
    session.add(sub)
    await session.flush()
    session.add(ConfirmationRequest(submission_id=sub.id, bot_message_id=905))
    await session.flush()
    channel = MagicMock(spec=discord.Thread)
    settings = _svc_settings(board)

    with patch("bot.discord_ingest.service._archive_thread_after_delay") as mock_delay:
        result = await handle_confirmation_reaction(
            session, settings=settings, channel=channel, message_id=905,
            member=None, user_id=999,
        )

    assert result is True
    assert sub.state == QUEUED
    mock_delay.assert_called_once()  # playlist opt-out means archival is unblocked


# ---------------------------------------------------------------------------
# _curator_authorized guards
# ---------------------------------------------------------------------------


async def test_curator_authorized_edge_cases(board):
    cfg = BoardConfig(
        name="robots", discord_guild_id=1, discord_channel_id=100,
        curator_role_ids=[7], curator_user_ids=[],
    )
    assert await _curator_authorized(MagicMock(), 1, None) is False

    dm_channel = MagicMock()
    dm_channel.guild = None
    assert await _curator_authorized(dm_channel, 1, cfg) is False

    channel = MagicMock()
    channel.guild.get_member.return_value = None
    channel.guild.fetch_member = AsyncMock(side_effect=discord.NotFound(MagicMock(), "gone"))
    assert await _curator_authorized(channel, 1, cfg) is False


# ---------------------------------------------------------------------------
# handle_cancel_reaction thread-less paths
# ---------------------------------------------------------------------------


async def test_cancel_reaction_published_without_thread(session, board):
    sub = make_submission(board, state=PUBLISHED)
    session.add(sub)
    await session.flush()
    session.add(CancellationRequest(submission_id=sub.id, bot_message_id=906))
    await session.flush()

    result = await handle_cancel_reaction(
        session, settings=_svc_settings(board), channel=MagicMock(),
        message_id=906, member=None, user_id=999,
    )

    assert result is None
    assert sub.state == PUBLISHED


async def test_cancel_reaction_no_thread_still_returns_source(session, board, tmp_path):
    sub = make_submission(board)
    sub.thread_id = 555
    session.add(sub)
    await session.flush()
    session.add(CancellationRequest(submission_id=sub.id, bot_message_id=907))
    await session.flush()
    channel = MagicMock()
    channel.guild = None  # thread cannot be resolved: notice is skipped

    result = await handle_cancel_reaction(
        session, settings=_svc_settings(board, tmp_dir=str(tmp_path)), channel=channel,
        message_id=907, member=None, user_id=999,
    )

    assert result == (board.discord_channel_id, 1)
    gone = await session.scalar(select(Submission).where(Submission.id == sub.id))
    assert gone is None


# ---------------------------------------------------------------------------
# playlist opt-out reaction: archival re-scheduling edges
# ---------------------------------------------------------------------------


async def test_playlist_opt_out_archived_thread_not_rescheduled(session, board):
    sub = make_submission(board, state=QUEUED)
    sub.thread_id = 650
    sub.playlist_opt_out_message_id = 9911
    session.add(sub)
    await session.flush()
    channel = MagicMock()
    channel.guild.get_thread.return_value = MagicMock(archived=True)
    scheduled = []

    with patch("bot.discord_ingest.service._fire_and_forget", scheduled.append):
        await handle_playlist_opt_out(
            session, message_id=9911, user_id=999, member=None,
            channel=channel, settings=_svc_settings(board), yt_client=None,
        )

    assert sub.playlist_skipped is True
    assert scheduled == []


async def test_playlist_opt_out_naive_queued_at_schedules_archive(session, board):
    sub = make_submission(board, state=QUEUED)
    sub.thread_id = 651
    sub.playlist_opt_out_message_id = 9912
    sub.updated_at = datetime(2020, 1, 1)  # naive: exercises the tzinfo backfill
    session.add(sub)
    await session.flush()
    channel = MagicMock()
    channel.guild.get_thread.return_value = MagicMock(archived=False)
    scheduled = []

    def fake_fire_and_forget(coro):
        scheduled.append(coro)
        coro.close()

    with patch("bot.discord_ingest.service._fire_and_forget", fake_fire_and_forget):
        await handle_playlist_opt_out(
            session, message_id=9912, user_id=999, member=None,
            channel=channel, settings=_svc_settings(board), yt_client=None,
        )

    assert len(scheduled) == 1


# ---------------------------------------------------------------------------
# button handlers: tombstone-edit failures and channel edges
# ---------------------------------------------------------------------------


async def test_cancel_button_tombstone_failure_and_cached_source_channel(session, board, tmp_path):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    interaction = make_interaction(custom_id=f"cancel:{sub.id}")
    interaction.message = MagicMock()
    interaction.message.edit = AsyncMock(side_effect=_forbidden())
    source_channel = MagicMock()
    source_channel.fetch_message = AsyncMock(return_value=MagicMock(clear_reaction=AsyncMock()))
    interaction.client = MagicMock()
    interaction.client.get_channel = MagicMock(return_value=source_channel)

    await handle_cancel_button(session, interaction, sub.id, _svc_settings(board, tmp_dir=str(tmp_path)))

    gone = await session.scalar(select(Submission).where(Submission.id == sub.id))
    assert gone is None
    # channel is not a Thread: no thread notice; cached source channel is used directly
    source_channel.fetch_message.assert_awaited_once()


async def test_confirm_button_skipped_playlist_edit_failure_no_channel(session, board):
    sub = make_submission(board, state=SubmissionState.READY_TO_QUEUE.value, source_waived=True)
    sub.playlist_skipped = True
    session.add(sub)
    await session.flush()
    session.add(ConfirmationRequest(submission_id=sub.id, bot_message_id=908))
    await session.flush()
    interaction = make_interaction()
    interaction.message = MagicMock()
    interaction.message.edit = AsyncMock(side_effect=_forbidden())
    interaction.channel = None

    await handle_confirm_button(session, interaction, sub.id, _svc_settings(board))

    assert sub.state == QUEUED


async def test_confirm_button_playlist_pending_blocks_archive(session, board):
    sub = make_submission(board, state=SubmissionState.READY_TO_QUEUE.value, source_waived=True)
    session.add(sub)
    await session.flush()
    session.add(ConfirmationRequest(submission_id=sub.id, bot_message_id=909))
    await session.flush()
    interaction = make_interaction()
    interaction.message = MagicMock()
    interaction.message.edit = AsyncMock()
    interaction.channel = MagicMock(spec=discord.Thread)
    settings = _svc_settings(board, youtube_playlist_id="PL1")

    with patch("bot.discord_ingest.service._archive_thread_after_delay") as mock_delay:
        await handle_confirm_button(session, interaction, sub.id, settings)

    assert sub.state == QUEUED
    mock_delay.assert_not_called()  # playlist auto-add hasn't recorded a row yet


async def test_metadata_confirm_button_tombstone_failure(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    session.add(MetadataRequest(submission_id=sub.id, bot_message_id=910))
    await session.flush()
    req = await session.scalar(select(MetadataRequest).where(MetadataRequest.submission_id == sub.id))
    interaction = make_interaction()
    interaction.message = MagicMock()
    interaction.message.edit = AsyncMock(side_effect=_forbidden())
    interaction.channel = MockDest()

    await handle_metadata_confirm_button(session, interaction, sub.id, _svc_settings(board))

    assert req.answer == "confirmed"
    assert replies.metadata_confirmed() in interaction.channel.sent


async def test_playlist_skip_button_edit_failure_no_channel(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    interaction = make_interaction()
    interaction.message = MagicMock()
    interaction.message.edit = AsyncMock(side_effect=_forbidden())
    interaction.channel = None

    await handle_playlist_skip_button(session, interaction, sub.id, _svc_settings(board))

    assert sub.playlist_skipped is True


async def test_playlist_skip_button_archived_thread_not_rescheduled(session, board):
    sub = make_submission(board, state=QUEUED)
    sub.thread_id = 700
    session.add(sub)
    await session.flush()
    interaction = make_interaction()
    interaction.message = MagicMock()
    interaction.message.edit = AsyncMock()
    interaction.channel = MagicMock()
    interaction.channel.guild.get_thread.return_value = MagicMock(archived=True)
    scheduled = []

    with patch("bot.discord_ingest.service._fire_and_forget", scheduled.append):
        await handle_playlist_skip_button(session, interaction, sub.id, _svc_settings(board))

    assert sub.playlist_skipped is True
    assert scheduled == []


async def test_playlist_skip_button_naive_queued_at_schedules_archive(session, board):
    sub = make_submission(board, state=QUEUED)
    sub.thread_id = 701
    sub.updated_at = datetime(2020, 1, 1)  # naive datetime path
    session.add(sub)
    await session.flush()
    interaction = make_interaction()
    interaction.message = MagicMock()
    interaction.message.edit = AsyncMock()
    interaction.channel = MagicMock()
    interaction.channel.guild.get_thread.return_value = MagicMock(archived=False)
    scheduled = []

    def fake_fire_and_forget(coro):
        scheduled.append(coro)
        coro.close()

    with patch("bot.discord_ingest.service._fire_and_forget", fake_fire_and_forget):
        await handle_playlist_skip_button(session, interaction, sub.id, _svc_settings(board))

    assert len(scheduled) == 1


# ---------------------------------------------------------------------------
# _ingest_content: embed and snapshot URL fallbacks
# ---------------------------------------------------------------------------


async def test_ingest_content_skips_urlless_and_duplicate_embeds(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    msg = InboundMessage(content="", embeds=[
        InboundEmbed(url=None),
        InboundEmbed(url="https://example.com/e1"),
        InboundEmbed(url="https://example.com/e1"),  # duplicate: skipped
    ])

    await _ingest_content(session, sub, msg, MagicMock(), AsyncMock())

    links = list(await session.scalars(
        select(SubmissionLink).where(SubmissionLink.submission_id == sub.id)
    ))
    assert [link.raw_url for link in links] == ["https://example.com/e1"]


async def test_ingest_content_snapshot_embed_url_fallback(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    msg = InboundMessage(content="forwarded without any link text", snapshots=[
        InboundSnapshot(content="", embeds=[
            InboundEmbed(url=None),
            InboundEmbed(url="https://example.com/snap"),
        ]),
    ])

    await _ingest_content(session, sub, msg, MagicMock(), AsyncMock())

    links = list(await session.scalars(
        select(SubmissionLink).where(SubmissionLink.submission_id == sub.id)
    ))
    assert [link.raw_url for link in links] == ["https://example.com/snap"]


# ---------------------------------------------------------------------------
# _resolve_links / _ingest_resolved_video / _ingest_attachment edges
# ---------------------------------------------------------------------------


async def test_resolve_links_no_image_url_skips_download(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    link = await _add_link(session, sub.id, "https://example.com/textonly")
    meta = ResolvedMetadata(title="Cool", via="oembed")  # no image_url, no video_url

    with patch("bot.discord_ingest.service.resolve", new_callable=AsyncMock, return_value=meta):
        await _resolve_links(session, sub, MagicMock(), AsyncMock())

    assert link.resolved_title == "Cool"
    assert link.resolved_image_path is None


async def test_ingest_resolved_video_missing_file_size_zero(session, board, tmp_path):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    link = await _add_link(session, sub.id, "https://example.com/vid")
    meta = ResolvedMetadata(video_url="https://cdn.example.com/src.mp4", video_width=10, video_height=10)
    ghost = str(tmp_path / "never-created.mp4")

    with patch("bot.discord_ingest.service.download_attachment", new_callable=AsyncMock, return_value=ghost), \
         patch("bot.discord_ingest.service._transcode_video", new_callable=AsyncMock, return_value=ghost):
        await _ingest_resolved_video(
            session, sub, link, meta, _svc_settings(board, tmp_dir=str(tmp_path)), AsyncMock()
        )

    att = await session.scalar(select(Attachment).where(Attachment.submission_id == sub.id))
    assert att is not None and att.is_video  # getsize OSError degrades to size 0, still attached


async def test_ingest_attachment_storage_full_leaves_undownloaded(session, board, tmp_path):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    att = InboundAttachment(id=5, url="https://cdn/img.png", filename="img.png", content_type="image/png")

    with patch(
        "bot.discord_ingest.service.download_attachment",
        new_callable=AsyncMock, side_effect=StorageFullError("disk full"),
    ):
        row = await _ingest_attachment(session, sub, att, _svc_settings(board, tmp_dir=str(tmp_path)), AsyncMock())

    assert row.local_path is None
    assert row.downloaded_at is None


# ---------------------------------------------------------------------------
# small pure helpers
# ---------------------------------------------------------------------------


def test_determine_kind_bluesky_record():
    link = MagicMock()
    link.domain_family = "bluesky"
    assert _determine_kind([link], has_uploaded_image=False) == "record"


def test_discord_file_reencodes_oversized_image(tmp_path):
    from PIL import Image

    # Random noise defeats PNG compression: the encoded buffer stays over 8 MB,
    # forcing the JPEG quality-70 fallback branch.
    path = tmp_path / "noise.png"
    Image.frombytes("RGB", (1920, 1920), os.urandom(1920 * 1920 * 3)).save(path, format="PNG")

    file = _discord_file_for_attachment(str(path), "noise.png")

    assert isinstance(file, discord.File)
    assert file.fp.getbuffer().nbytes <= 8 * 1024 * 1024


def test_is_authorized_non_op_without_board_cfg():
    author = MagicMock()
    author.id = 5
    submission = MagicMock()
    submission.author_id = 999
    assert _is_authorized(author, submission, None) is False


# ---------------------------------------------------------------------------
# _resolve_parent_ref standalone fallbacks
# ---------------------------------------------------------------------------


async def test_resolve_parent_ref_no_parent_submission(session, board):
    sub = make_submission(board, reply_to_discord_message_id=111_111)
    session.add(sub)
    await session.flush()
    assert await _resolve_parent_ref(session, sub) is None


async def test_resolve_parent_ref_attempt_missing_cid(session, board):
    parent = make_submission(board, state=PUBLISHED, source_discord_message_id=210)
    session.add(parent)
    await session.flush()
    session.add(PublishAttempt(
        submission_id=parent.id, success=True, at_uri="at://did/p", at_cid=None,
    ))
    child = make_submission(board, source_discord_message_id=211, reply_to_discord_message_id=210)
    session.add(child)
    await session.flush()

    assert await _resolve_parent_ref(session, child) is None


# ---------------------------------------------------------------------------
# recompute_and_request: Discord-send failure swallowing per request type
# ---------------------------------------------------------------------------


async def test_recompute_send_failures_ready_submission(session, board):
    """Cancel, supplemental image/link, and confirmation sends all fail quietly."""
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    await _add_link(
        session, sub.id, "https://example.com/ready",
        resolved_title="T", resolved_via="oembed", resolved_image_path="/tmp/thumb.jpg",
    )

    state = await recompute_and_request(
        session, sub, settings=_svc_settings(board), destination=RaisingDest()
    )

    assert state == SubmissionState.READY_TO_QUEUE
    conf = await session.scalar(select(ConfirmationRequest).where(ConfirmationRequest.submission_id == sub.id))
    assert conf is None  # send failed, so no request row was recorded


async def test_recompute_send_failures_source_and_graphic(session, board):
    sub = make_submission(board, graphic_classification_required=True)
    session.add(sub)
    await session.flush()

    state = await recompute_and_request(
        session, sub, settings=_svc_settings(board, require_graphic=True), destination=RaisingDest()
    )

    assert state == SubmissionState.AWAITING_SOURCE
    label = await session.scalar(select(ContentLabelRequest).where(ContentLabelRequest.submission_id == sub.id))
    assert label is None


async def test_recompute_send_failure_metadata_request(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    await _add_link(session, sub.id, "https://example.com/unresolved")  # resolved_via None

    state = await recompute_and_request(
        session, sub, settings=_svc_settings(board), destination=RaisingDest()
    )

    assert state == SubmissionState.AWAITING_BETTER_LINK
    req = await session.scalar(select(MetadataRequest).where(MetadataRequest.submission_id == sub.id))
    assert req is None


async def test_recompute_send_failure_image_request(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    await _add_link(
        session, sub.id, "https://example.com/no-thumb",
        resolved_title="T", resolved_via="opengraph",  # metadata fine, image missing
    )

    state = await recompute_and_request(
        session, sub, settings=_svc_settings(board), destination=RaisingDest()
    )

    assert state == SubmissionState.AWAITING_IMAGE


async def test_recompute_alt_text_send_failure_and_non_media_skip(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    session.add(Attachment(
        submission_id=sub.id, discord_attachment_id=1, filename="pic.jpg",
        discord_url="https://cdn/pic.jpg", mime="image/jpeg",
        is_image=True, is_video=False,
        alt_text_status=AltTextStatus.NEEDED.value,
    ))
    session.add(Attachment(
        submission_id=sub.id, discord_attachment_id=2, filename="doc.pdf",
        discord_url="https://cdn/doc.pdf", mime="application/pdf",
        is_image=False, is_video=False,
        alt_text_status=AltTextStatus.NOT_REQUIRED.value,
    ))
    await session.flush()

    await recompute_and_request(
        session, sub, settings=_svc_settings(board), destination=RaisingDest()
    )

    reqs = list(await session.scalars(
        select(AttachmentAltTextRequest).where(AttachmentAltTextRequest.submission_id == sub.id)
    ))
    assert reqs == []  # send failed for the image; the PDF was skipped entirely


async def test_recompute_alt_text_preview_success_and_fallback(session, board, tmp_path):
    from PIL import Image

    good_path = tmp_path / "ok.jpg"
    Image.new("RGB", (4, 4), "red").save(good_path, format="JPEG")
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    session.add(Attachment(
        submission_id=sub.id, discord_attachment_id=1, filename="ok.jpg",
        discord_url="https://cdn/ok.jpg", mime="image/jpeg",
        is_image=True, is_video=False,
        alt_text_status=AltTextStatus.NEEDED.value, local_path=str(good_path),
    ))
    session.add(Attachment(
        submission_id=sub.id, discord_attachment_id=2, filename="broken.jpg",
        discord_url="https://cdn/broken.jpg", mime="image/jpeg",
        is_image=True, is_video=False,
        alt_text_status=AltTextStatus.NEEDED.value,
        local_path=str(tmp_path / "missing.jpg"),  # preview build raises, URL fallback used
    ))
    await session.flush()
    dest = MockDest()

    await recompute_and_request(session, sub, settings=_svc_settings(board), destination=dest)

    reqs = list(await session.scalars(
        select(AttachmentAltTextRequest).where(AttachmentAltTextRequest.submission_id == sub.id)
    ))
    assert len(reqs) == 2
    assert any("https://cdn/broken.jpg" in m for m in dest.sent)


async def test_recompute_from_reply_updated_notice_failure_still_archives(session, board):
    sub = make_submission(board, state=QUEUED)
    session.add(sub)
    await session.flush()
    await _add_link(
        session, sub.id, "https://example.com/queued",
        resolved_title="T", resolved_via="oembed", resolved_image_path="/tmp/thumb.jpg",
    )
    dest = RaisingDest()

    await recompute_and_request(
        session, sub, settings=_svc_settings(board), destination=dest, from_reply=True
    )

    assert dest.archived == [replies.closing_notice("updated")]


# ---------------------------------------------------------------------------
# _attempt_publish edges
# ---------------------------------------------------------------------------


async def test_attempt_publish_no_board_handle_fails(session, board):
    settings = MagicMock()
    settings.board_for_channel.return_value = None
    sub = make_submission(board, state=QUEUED)
    session.add(sub)
    await session.flush()

    result = await publish_queued_submission(session, settings, sub, RaisingDest())

    assert result is PublishOutcome.FAILED
    assert sub.state == SubmissionState.PUBLISH_FAILED.value
    attempt = await session.scalar(select(PublishAttempt).where(PublishAttempt.submission_id == sub.id))
    assert "no Bluesky handle" in attempt.error


async def test_attempt_publish_no_password_notice_failure_swallowed(session, board):
    sub = make_submission(board, state=QUEUED)
    session.add(sub)
    await session.flush()

    result = await publish_queued_submission(
        session, _svc_settings(board, password=None), sub, RaisingDest()
    )

    assert result is PublishOutcome.FAILED
    attempt = await session.scalar(select(PublishAttempt).where(PublishAttempt.submission_id == sub.id))
    assert "no app password" in attempt.error


async def test_attempt_publish_skips_null_canonical_link(session, board):
    sub = make_submission(board, state=QUEUED)
    session.add(sub)
    await session.flush()
    # In-memory only: a null canonical URL must not join against other nulls.
    link = SubmissionLink(
        submission_id=sub.id, order_index=0, raw_url="raw-only",
        canonical_url=None, domain_family="other",
    )
    ok = PublishResult(
        success=True, at_uri="at://did/new", at_cid="c", bsky_url="https://bsky.app/new",
    )

    with patch("bot.publish.publish_submission", new_callable=AsyncMock, return_value=ok) as mock_pub:
        result = await _attempt_publish(session, _svc_settings(board), sub, [], [link], MockDest())

    assert result is PublishOutcome.PUBLISHED
    mock_pub.assert_awaited_once()


# ---------------------------------------------------------------------------
# _build_post_preview: reply URL derived from at_uri when bsky_url is absent
# ---------------------------------------------------------------------------


async def test_preview_reply_url_from_at_uri(session, board):
    parent = make_submission(board, state=PUBLISHED, source_discord_message_id=610)
    session.add(parent)
    await session.flush()
    session.add(PublishAttempt(
        submission_id=parent.id, success=True,
        at_uri="at://did:plc:p/app.bsky.feed.post/rk", at_cid="c", bsky_url=None,
    ))
    child = make_submission(board, source_discord_message_id=611, reply_to_discord_message_id=610)
    session.add(child)
    await session.flush()

    preview = await _build_post_preview(session, child, [], [])

    assert preview.reply_to_bsky_url == "https://bsky.app/profile/did:plc:p/post/rk"


# ---------------------------------------------------------------------------
# _apply_answer fall-through paths
# ---------------------------------------------------------------------------


def _reply_message(content: str) -> MagicMock:
    msg = MagicMock(spec=discord.Message)
    msg.content = content
    msg.attachments = []
    msg.author = MagicMock()
    msg.author.id = 999
    msg.reply = AsyncMock()
    return msg


async def test_apply_answer_alt_text_missing_attachment(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    req = AttachmentAltTextRequest(submission_id=sub.id, attachment_id=424_242, bot_message_id=911)
    message = _reply_message("a fine description")

    handled = await _apply_answer(session, req, sub, message, MagicMock(), AsyncMock())

    assert handled is True
    assert req.answered_at is not None  # answer recorded even though the row is gone


async def test_apply_answer_unknown_request_type_falls_through(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    req = MagicMock()  # matches none of the request model types
    message = _reply_message("free text")

    handled = await _apply_answer(session, req, sub, message, MagicMock(), AsyncMock())

    assert handled is True
    assert req.answer == "free text"


async def test_apply_answer_metadata_without_primary_link(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    req = MetadataRequest(submission_id=sub.id, bot_message_id=912)
    message = _reply_message("https://example.com/better")

    handled = await _apply_answer(session, req, sub, message, MagicMock(), AsyncMock())

    assert handled is True
    message.reply.assert_awaited_once()  # link-updated ack sent even with no primary link
    assert req.answered_at is not None
