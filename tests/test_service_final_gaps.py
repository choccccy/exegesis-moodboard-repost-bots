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
from bot.curation import replies
from bot.discord_ingest.service import _archive_thread_after_delay, _find_publish_time_duplicate, _discord_file_for_attachment, _post_thread_anchor, _resolve_thread, ensure_thread_persisted, handle_reaction, publish_queued_submission
from bot.curation.ingest import _AttachmentPlan, _IngestPlan, _LinkPlan, _attach_resolved_video, _download_attachment_file, _download_resolved_video, _gather_ingest, _persist_ingest_outcome, _persist_ingest_skeletons
from bot.curation.statemachine import _build_post_preview, _determine_kind, _resolve_parent_ref, recompute_and_request
from bot.curation.handlers import _apply_answer, _is_authorized, handle_cancel_button, handle_cancel_reaction, handle_confirm_button, handle_confirmation_reaction, handle_label_reaction, handle_metadata_confirm_button, handle_metadata_reaction, handle_playlist_opt_out, handle_playlist_skip_button, handle_reaction_removed
from bot.curation.types import InboundAttachment, InboundEmbed, InboundMessage, InboundSnapshot
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

from bot.curation.events import InteractionEvent, ReactionEvent, ReplyEvent
from bot.curation.outcomes import Ack, Noop, Tombstone
from bot.curation.surface import SurfaceError
from conftest import MockDest, make_interaction, make_submission


def _event(user_id: int, submission_id: int):
    return InteractionEvent(user_id=user_id, submission_id=submission_id, member=None)


def _reaction_event(user_id: int, message_id: int, *, emoji: str = "\N{DROP OF BLOOD}",
                    channel_id: int = 100):
    return ReactionEvent(user_id=user_id, message_id=message_id, channel_id=channel_id,
                         emoji=emoji, member=None)

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
    """Surface whose send() always raises SurfaceError (what a real DiscordSurface
    raises when a post is forbidden / the platform errors); archive records."""

    def __init__(self):
        self.archived: list[str] = []

    async def send(self, content=None, **kwargs):
        raise SurfaceError("forbidden")

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


async def test_handle_reaction_ignores_unwatched_channel(session, board, bind_db_scopes):
    msg = _message(channel_id=555_555)  # no Board row for this channel
    result = await handle_reaction(
        settings=MagicMock(), message=msg, http_client=AsyncMock(), skip_auth=True
    )
    assert result is False
    msg.channel.create_thread.assert_not_called()


async def test_handle_reaction_rejects_non_curator(session, board, bind_db_scopes):
    msg = _message(channel_id=board.discord_channel_id)
    settings = _svc_settings(board)  # empty curator lists
    result = await handle_reaction(
        settings=settings, message=msg, http_client=AsyncMock(),
        member=None, user_id=12345, skip_auth=False,
    )
    assert result is False
    sub = await session.scalar(select(Submission))
    assert sub is None


async def test_ensure_thread_persisted_missing_submission_returns_none(session, board, bind_db_scopes):
    """The submission vanished between DB scopes → (None, False), no thread work."""
    msg = _message(channel_id=board.discord_channel_id)
    thread, is_new = await ensure_thread_persisted(
        settings=_svc_settings(board), message=msg, submission_id=999_999, post_anchor=False,
    )
    assert thread is None
    assert is_new is False
    msg.channel.create_thread.assert_not_called()


def test_archive_thread_after_delay_schedules_background_task():
    """The wrapper hands the delayed-archive coroutine to _fire_and_forget."""
    thread = MagicMock()
    with patch("bot.discord_ingest.service._archive_thread_after_delay_seconds",
               new=MagicMock(return_value="CORO")) as mock_coro, \
         patch("bot.discord_ingest.service._fire_and_forget") as mock_fire:
        _archive_thread_after_delay(thread, notice="bye")
    mock_coro.assert_called_once()
    mock_fire.assert_called_once_with("CORO")


async def test_handle_reaction_allows_author_self_react(session, board, bind_db_scopes):
    """The OP may 🦋 their own post even without curator rights (#66)."""
    msg = _message(channel_id=board.discord_channel_id)  # author_id=999
    msg.channel.create_thread.return_value = _thread(thread_id=650)
    settings = _svc_settings(board)  # empty curator lists

    result = await handle_reaction(
        settings=settings, message=msg, http_client=AsyncMock(),
        member=None, user_id=msg.author.id, skip_auth=False,
    )

    assert result is not False
    sub = await session.scalar(select(Submission))
    assert sub is not None
    assert sub.author_id == msg.author.id


async def test_handle_reaction_duplicate_of_published_closes_thread(session, board, bind_db_scopes):
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

    with patch("bot.curation.ingest.resolve", new_callable=AsyncMock, return_value=ResolvedMetadata(via="none")), \
         patch("bot.discord_ingest.service.remove_submission_dir"), \
         patch("bot.discord_ingest.service._clear_trigger_reaction", new_callable=AsyncMock), \
         patch("bot.discord_ingest.service._archive_thread", new_callable=AsyncMock) as mock_archive:
        result = await handle_reaction(
            settings=_svc_settings(board), message=msg,
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


def _removed_event(user_id: int, message_id: int, board):
    return _reaction_event(user_id, message_id, emoji="🦋", channel_id=board.discord_channel_id)


async def test_reaction_removed_published_without_thread_noop(session, board):
    sub = make_submission(board, state=PUBLISHED, source_discord_message_id=61)
    session.add(sub)
    await session.flush()
    settings = _svc_settings(board, curator_user_ids=[42])

    await handle_reaction_removed(session, _removed_event(42, 61, board), MockDest(), settings)

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
    dest = MockDest()
    settings = _svc_settings(board, curator_user_ids=[42])

    await handle_reaction_removed(session, _removed_event(42, 62, board), dest, settings)

    assert "bsky.app/profile/robots.exegesis.space/post/rr" in dest.sent[0]


async def test_reaction_removed_published_no_attempt_generic_name(session, board):
    sub = make_submission(board, state=PUBLISHED, source_discord_message_id=63)
    sub.thread_id = 501
    session.add(sub)
    await session.flush()
    dest = MockDest()
    settings = _svc_settings(board, curator_user_ids=[42])

    await handle_reaction_removed(session, _removed_event(42, 63, board), dest, settings)

    assert dest.sent[0] == replies.cannot_remove_published("Bluesky")


# ---------------------------------------------------------------------------
# label / metadata / confirmation reaction guards
# ---------------------------------------------------------------------------


async def test_label_reaction_missing_submission(session, board):
    session.add(ContentLabelRequest(submission_id=999_999, bot_message_id=901))
    await session.flush()
    dest = MockDest()

    await handle_label_reaction(
        session, _reaction_event(1, 901), dest, MagicMock(),
    )

    assert not dest.sent


async def test_label_reaction_unauthorized(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    session.add(ContentLabelRequest(submission_id=sub.id, bot_message_id=902))
    await session.flush()
    settings = MagicMock()
    settings.board_for_channel.return_value = None

    await handle_label_reaction(
        session, _reaction_event(55, 902), MockDest(), settings,  # not OP, no curators
    )

    assert sub.graphic_status == "unknown"


async def test_metadata_reaction_no_open_request(session, board):
    dest = MockDest()
    await handle_metadata_reaction(
        session, _reaction_event(1, 54321, emoji="🔗"), dest, MagicMock(),
    )
    assert not dest.sent


async def test_metadata_reaction_missing_submission(session, board):
    session.add(MetadataRequest(submission_id=999_999, bot_message_id=903))
    await session.flush()
    dest = MockDest()

    await handle_metadata_reaction(
        session, _reaction_event(1, 903, emoji="🔗"), dest, MagicMock(),
    )

    assert not dest.sent


async def test_confirmation_reaction_terminal_state_returns_false(session, board):
    sub = make_submission(board, state=QUEUED)
    session.add(sub)
    await session.flush()
    session.add(ConfirmationRequest(submission_id=sub.id, bot_message_id=904))
    await session.flush()

    result = await handle_confirmation_reaction(
        session, _reaction_event(999, 904, emoji="✅"), MockDest(), MagicMock(),
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
    dest = MockDest()
    settings = _svc_settings(board)

    result = await handle_confirmation_reaction(
        session, _reaction_event(999, 905, emoji="✅"), dest, settings,
    )

    assert result is True
    assert sub.state == QUEUED
    assert dest.archive_delays  # playlist opt-out means archival is unblocked


# ---------------------------------------------------------------------------
# handle_cancel_reaction thread-less paths
# ---------------------------------------------------------------------------


async def test_cancel_reaction_published_keeps_row(session, board):
    sub = make_submission(board, state=PUBLISHED)
    session.add(sub)
    await session.flush()
    session.add(CancellationRequest(submission_id=sub.id, bot_message_id=906))
    await session.flush()
    dest = MockDest()

    await handle_cancel_reaction(
        session, _reaction_event(999, 906, emoji="❌"), dest, _svc_settings(board),
    )

    assert sub.state == PUBLISHED
    assert dest.sent  # cannot-remove-published notice
    assert not dest.cleared_triggers


async def test_cancel_reaction_deletes_and_clears_trigger(session, board, tmp_path):
    sub = make_submission(board)
    sub.thread_id = 555
    session.add(sub)
    await session.flush()
    session.add(CancellationRequest(submission_id=sub.id, bot_message_id=907))
    await session.flush()
    dest = MockDest()

    await handle_cancel_reaction(
        session, _reaction_event(999, 907, emoji="❌"), dest,
        _svc_settings(board, tmp_dir=str(tmp_path)),
    )

    assert dest.cleared_triggers == [(board.discord_channel_id, 1)]
    gone = await session.scalar(select(Submission).where(Submission.id == sub.id))
    assert gone is None


# ---------------------------------------------------------------------------
# playlist opt-out reaction: archival re-scheduling edges
# ---------------------------------------------------------------------------


async def test_playlist_opt_out_queued_schedules_archive(session, board):
    # Opting out of a QUEUED submission with a thread re-arms archival through the
    # port. (Re-arming an already-archived thread is a harmless no-op, so the old
    # not-archived guard was dropped in the surface migration.)
    sub = make_submission(board, state=QUEUED)
    sub.thread_id = 651
    sub.playlist_opt_out_message_id = 9912
    sub.updated_at = datetime(2020, 1, 1)  # naive updated_at input
    session.add(sub)
    await session.flush()
    dest = MockDest()

    await handle_playlist_opt_out(
        session, _reaction_event(999, 9912, emoji="⏹️"), dest, _svc_settings(board),
    )

    assert sub.playlist_skipped is True
    assert len(dest.archive_delays) == 1


# ---------------------------------------------------------------------------
# button handlers: tombstone-edit failures and channel edges
# ---------------------------------------------------------------------------


async def test_cancel_button_deletes_and_clears_trigger(session, board, tmp_path):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()

    dest = MockDest()
    outcome = await handle_cancel_button(
        session, _event(999, sub.id), dest, _svc_settings(board, tmp_dir=str(tmp_path))
    )

    gone = await session.scalar(select(Submission).where(Submission.id == sub.id))
    assert gone is None
    assert isinstance(outcome, Tombstone)
    assert dest.cleared_triggers  # trigger reaction cleared on the source (via the Surface)


async def test_confirm_button_skipped_playlist_queues(session, board):
    sub = make_submission(board, state=SubmissionState.READY_TO_QUEUE.value, source_waived=True)
    sub.playlist_skipped = True
    session.add(sub)
    await session.flush()
    session.add(ConfirmationRequest(submission_id=sub.id, bot_message_id=908))
    await session.flush()

    outcome = await handle_confirm_button(session, _event(999, sub.id), MockDest(), _svc_settings(board))

    assert sub.state == QUEUED
    assert isinstance(outcome, Tombstone)


async def test_confirm_button_playlist_pending_blocks_archive(session, board):
    sub = make_submission(board, state=SubmissionState.READY_TO_QUEUE.value, source_waived=True)
    session.add(sub)
    await session.flush()
    session.add(ConfirmationRequest(submission_id=sub.id, bot_message_id=909))
    await session.flush()
    dest = MockDest()
    settings = _svc_settings(board, youtube_playlist_id="PL1")

    await handle_confirm_button(session, _event(999, sub.id), dest, settings)

    assert sub.state == QUEUED
    assert not dest.archived  # playlist auto-add hasn't recorded a row yet -> not close-ready


async def test_metadata_confirm_button_posts_notice(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    session.add(MetadataRequest(submission_id=sub.id, bot_message_id=910))
    await session.flush()
    req = await session.scalar(select(MetadataRequest).where(MetadataRequest.submission_id == sub.id))

    dest = MockDest()
    outcome = await handle_metadata_confirm_button(session, _event(999, sub.id), dest, _svc_settings(board))

    assert req.answer == "confirmed"
    assert replies.metadata_confirmed() in dest.sent
    assert isinstance(outcome, Tombstone)


async def test_playlist_skip_button_sets_flag(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()

    outcome = await handle_playlist_skip_button(session, _event(999, sub.id), MockDest(), _svc_settings(board))

    assert sub.playlist_skipped is True
    assert isinstance(outcome, Tombstone)


async def test_playlist_skip_button_naive_queued_at_schedules_archive(session, board):
    sub = make_submission(board, state=QUEUED)
    sub.thread_id = 701
    sub.updated_at = datetime(2020, 1, 1)  # naive datetime path: normalized to aware, long past
    session.add(sub)
    await session.flush()

    dest = MockDest()
    await handle_playlist_skip_button(session, _event(999, sub.id), dest, _svc_settings(board))

    # The naive updated_at must be normalized (not crash) and the archive re-armed.
    assert len(dest.archive_delays) == 1
    assert isinstance(dest.archive_delays[0], float)


# ---------------------------------------------------------------------------
# _persist_ingest_skeletons: embed and snapshot URL fallbacks
# ---------------------------------------------------------------------------


async def test_skeletons_skip_urlless_and_duplicate_embeds(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    msg = InboundMessage(content="", embeds=[
        InboundEmbed(url=None),
        InboundEmbed(url="https://example.com/e1"),
        InboundEmbed(url="https://example.com/e1"),  # duplicate: skipped
    ])

    await _persist_ingest_skeletons(session, sub, msg)

    links = list(await session.scalars(
        select(SubmissionLink).where(SubmissionLink.submission_id == sub.id)
    ))
    assert [link.raw_url for link in links] == ["https://example.com/e1"]


async def test_skeletons_snapshot_embed_url_fallback(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    msg = InboundMessage(content="forwarded without any link text", snapshots=[
        InboundSnapshot(content="", embeds=[
            InboundEmbed(url=None),
            InboundEmbed(url="https://example.com/snap"),
        ]),
    ])

    await _persist_ingest_skeletons(session, sub, msg)

    links = list(await session.scalars(
        select(SubmissionLink).where(SubmissionLink.submission_id == sub.id)
    ))
    assert [link.raw_url for link in links] == ["https://example.com/snap"]


# ---------------------------------------------------------------------------
# gather / video-download / attachment-download edges
# ---------------------------------------------------------------------------


async def test_gather_no_image_url_skips_download(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    link = await _add_link(session, sub.id, "https://example.com/textonly")
    meta = ResolvedMetadata(title="Cool", via="oembed")  # no image_url, no video_url
    plan = _IngestPlan(
        submission_id=sub.id, board_id=sub.board_id,
        link_plans=[_LinkPlan(link_id=link.id, canonical_url=link.canonical_url,
                              domain_family=link.domain_family, is_primary=True)],
        att_plans=[], embed_title=None, embed_description=None, embed_thumb_url=None,
        thumb_proxy_url=None, has_existing_video=False,
    )

    with patch("bot.curation.ingest.resolve", new_callable=AsyncMock, return_value=meta):
        outcome = await _gather_ingest(plan, _svc_settings(board), AsyncMock())
    await _persist_ingest_outcome(session, outcome, sub.id)

    assert link.resolved_title == "Cool"
    assert link.resolved_image_path is None


async def test_resolved_video_missing_file_size_zero(session, board, tmp_path):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    link = await _add_link(session, sub.id, "https://example.com/vid")
    meta = ResolvedMetadata(video_url="https://cdn.example.com/src.mp4", video_width=10, video_height=10)
    ghost = str(tmp_path / "never-created.mp4")
    lp = _LinkPlan(link_id=link.id, canonical_url=link.canonical_url, domain_family="other", is_primary=True)

    with patch("bot.curation.ingest.download_attachment", new_callable=AsyncMock, return_value=ghost), \
         patch("bot.curation.ingest._transcode_video", new_callable=AsyncMock, return_value=ghost):
        path = await _download_resolved_video(lp, meta, str(tmp_path), _svc_settings(board, tmp_dir=str(tmp_path)), AsyncMock())

    # getsize OSError on the never-created file degrades to size 0 -> under limit -> kept.
    assert path == ghost
    created = await _attach_resolved_video(
        session, sub.id, link.id, video_url=meta.video_url,
        video_width=10, video_height=10, video_path=path,
    )
    assert created
    att = await session.scalar(select(Attachment).where(Attachment.submission_id == sub.id))
    assert att is not None and att.is_video


async def test_download_attachment_storage_full_returns_none(session, board, tmp_path):
    plan = _AttachmentPlan(row_id=5, url="https://cdn/img.png", filename="img.png", is_video=False)

    with patch(
        "bot.curation.ingest.download_attachment",
        new_callable=AsyncMock, side_effect=StorageFullError("disk full"),
    ):
        path = await _download_attachment_file(plan, str(tmp_path), _svc_settings(board, tmp_dir=str(tmp_path)), AsyncMock())

    assert path is None


# ---------------------------------------------------------------------------
# small pure helpers
# ---------------------------------------------------------------------------


def test_determine_kind_bluesky_record():
    link = MagicMock()
    link.domain_family = "bluesky"
    link.canonical_url = "https://bsky.app/profile/alice.bsky.social/post/abc123"
    assert _determine_kind([link], has_uploaded_image=False) == "record"


def test_determine_kind_bluesky_profile_is_external():
    # A bare bsky profile link is a source link, not a native repost (#62).
    link = MagicMock()
    link.domain_family = "bluesky"
    link.canonical_url = "https://bsky.app/profile/alice.bsky.social"
    assert _determine_kind([link], has_uploaded_image=False) == "external"


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
    submission = MagicMock()
    submission.author_id = 999
    assert _is_authorized(None, 5, submission, None) is False


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
        sub.id, settings=_svc_settings(board), destination=RaisingDest(), ambient_session=session
    )

    assert state == SubmissionState.READY_TO_QUEUE
    conf = await session.scalar(select(ConfirmationRequest).where(ConfirmationRequest.submission_id == sub.id))
    assert conf is None  # send failed, so no request row was recorded


async def test_recompute_send_failures_source_and_graphic(session, board):
    sub = make_submission(board, graphic_classification_required=True)
    session.add(sub)
    await session.flush()

    state = await recompute_and_request(
        sub.id, settings=_svc_settings(board, require_graphic=True), destination=RaisingDest(), ambient_session=session
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
        sub.id, settings=_svc_settings(board), destination=RaisingDest(), ambient_session=session
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
        sub.id, settings=_svc_settings(board), destination=RaisingDest(), ambient_session=session
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
        sub.id, settings=_svc_settings(board), destination=RaisingDest(), ambient_session=session
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

    await recompute_and_request(sub.id, settings=_svc_settings(board), destination=dest, ambient_session=session)

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
        sub.id, settings=_svc_settings(board), destination=dest, from_reply=True, ambient_session=session
    )

    assert dest.archived == [replies.closing_notice("updated")]


# ---------------------------------------------------------------------------
# _attempt_publish edges
# ---------------------------------------------------------------------------


async def test_attempt_publish_no_board_handle_fails(session, board, bind_db_scopes):
    settings = MagicMock()
    settings.board_for_channel.return_value = None
    sub = make_submission(board, state=QUEUED)
    session.add(sub)
    await session.flush()

    result = await publish_queued_submission(settings, sub.id, RaisingDest())

    # Board-wide config gap: block (no auto-retry) and abandon the tick.
    assert result is PublishOutcome.UNAVAILABLE
    assert sub.state == SubmissionState.PUBLISH_BLOCKED.value
    attempt = await session.scalar(select(PublishAttempt).where(PublishAttempt.submission_id == sub.id))
    assert "no Bluesky handle" in attempt.error


async def test_attempt_publish_no_password_notice_failure_swallowed(session, board, bind_db_scopes):
    sub = make_submission(board, state=QUEUED)
    session.add(sub)
    await session.flush()

    result = await publish_queued_submission(
        _svc_settings(board, password=None), sub.id, RaisingDest()
    )

    assert result is PublishOutcome.UNAVAILABLE
    attempt = await session.scalar(select(PublishAttempt).where(PublishAttempt.submission_id == sub.id))
    assert "no app password" in attempt.error


async def test_find_publish_time_duplicate_skips_null_canonical_link(session, board):
    sub = make_submission(board, state=QUEUED)
    session.add(sub)
    await session.flush()
    # canonical_url is NOT NULL in the schema, so this state can't occur via the DB;
    # the in-memory link exercises the defensive guard that skips null canonical_urls
    # in the dup check (so a null wouldn't SQL-join against other nulls).
    link = SubmissionLink(
        submission_id=sub.id, order_index=0, raw_url="raw-only",
        canonical_url=None, domain_family="other",
    )

    dup = await _find_publish_time_duplicate(session, sub, [link])

    assert dup is None


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


def _reply_event(content: str) -> ReplyEvent:
    from bot.curation.types import InboundMessage
    return ReplyEvent(bot_message_id=1, author_id=999, member=None,
                      message=InboundMessage(content=content))


async def test_apply_answer_alt_text_missing_attachment(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    req = AttachmentAltTextRequest(submission_id=sub.id, attachment_id=424_242, bot_message_id=911)

    handled = await _apply_answer(
        session, req, sub, _reply_event("a fine description"), MockDest(), MagicMock(), AsyncMock(),
    )

    assert handled is True
    assert req.answered_at is not None  # answer recorded even though the row is gone


async def test_apply_answer_unknown_request_type_falls_through(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    req = MagicMock()  # matches none of the request model types

    handled = await _apply_answer(
        session, req, sub, _reply_event("free text"), MockDest(), MagicMock(), AsyncMock(),
    )

    assert handled is True
    assert req.answer == "free text"


async def test_apply_answer_metadata_without_primary_link(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    req = MetadataRequest(submission_id=sub.id, bot_message_id=912)
    dest = MockDest()

    handled = await _apply_answer(
        session, req, sub, _reply_event("https://example.com/better"), dest, MagicMock(), AsyncMock(),
    )

    assert handled is True
    assert dest.sent  # link-updated ack sent even with no primary link
    assert req.answered_at is not None
