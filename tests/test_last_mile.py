"""Last-mile branch coverage: the final dozen statements across five modules.

Each test here exists to pin one specific rarely-taken branch - deleted-row
guards, canonicalizers with no other callers, post-type classification, and
SQLite's naive-datetime round-trip in the thread-archive rescheduling paths.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from sqlalchemy import select

from bot.admin.backfill_videos import amain, find_candidates
from bot.canonicalize import canonicalize
from bot.dashboard.queries import board_queue, board_stats, recent_publishes
from bot.discord_ingest import replies
from bot.discord_ingest.service import handle_playlist_opt_out, handle_playlist_skip_button
from bot.models import PublishAttempt, Submission, SubmissionLink
from bot.state import SubmissionState

from conftest import make_submission


# ---------------------------------------------------------------------------
# backfill_videos: deleted-row guards
# ---------------------------------------------------------------------------


async def test_backfill_candidate_with_null_canonical_url_skipped(session, board):
    sub = make_submission(board, state=SubmissionState.QUEUED.value, source_discord_message_id=901)
    session.add(sub)
    await session.flush()
    session.add(SubmissionLink(
        submission_id=sub.id, order_index=0,
        raw_url="https://www.tiktok.com/@u/video/1", canonical_url="",
        domain_family="tiktok",
    ))
    await session.flush()

    ids = [c[0] for c in await find_candidates(session)]
    assert sub.id not in ids


async def test_backfill_amain_tolerates_row_deleted_mid_run(global_engine, caplog):
    from bot.resolve import ResolvedMetadata

    settings = MagicMock()
    settings.youtube_api_key = None
    meta = ResolvedMetadata(video_url="https://video.example/v.mp4")
    with patch("bot.admin.backfill_videos.get_settings", return_value=settings), \
         patch("bot.admin.backfill_videos.init_engine"), \
         patch("bot.admin.backfill_videos.dispose_engine", new=AsyncMock()), \
         patch("bot.admin.backfill_videos._SLEEP_BETWEEN_CALLS", 0), \
         patch("bot.admin.backfill_videos.resolve", new=AsyncMock(return_value=meta)), \
         patch("bot.admin.backfill_videos.find_candidates",
               new=AsyncMock(return_value=[(99999, 88888, "twitter", "https://x.com/u/status/1")])):
        await amain(dry_run=False, limit=None)  # rows 99999/88888 do not exist: guard skips


# ---------------------------------------------------------------------------
# canonicalize: deviantart short link, furaffinity, substack
# ---------------------------------------------------------------------------


def test_canonicalize_deviantart_fav_me_short_link():
    result = canonicalize("https://fav.me/dabcdef?ref=share")
    assert result.canonical_url == "https://www.deviantart.com/dabcdef"
    assert result.domain_family == "deviantart"


def test_canonicalize_furaffinity_strips_query():
    result = canonicalize("http://furaffinity.net/view/12345/?upload-successful")
    assert result.canonical_url == "https://www.furaffinity.net/view/12345"
    assert result.domain_family == "furaffinity"


def test_canonicalize_substack_preserves_subdomain():
    result = canonicalize("https://someauthor.substack.com/p/cool-essay?utm_source=share")
    assert result.canonical_url == "https://someauthor.substack.com/p/cool-essay"
    assert result.domain_family == "substack"


# ---------------------------------------------------------------------------
# dashboard queries: no-attempts board and post_type classification
# ---------------------------------------------------------------------------


class _StatSettings:
    queue_timezone = "America/Denver"
    queue_fresh_window_hours = 72
    queue_target_days = 90
    queue_min_daily = 1
    queue_max_daily = 6
    boards = []
    data_dir = "/tmp/nonexistent-status"

    def bluesky_handle_for(self, name):
        return f"{name}.exegesis.space"


async def test_board_stats_board_without_any_publishes(session, board):
    stats = await board_stats(session, _StatSettings())
    stat = next(s for s in stats if s.name == board.name)
    assert stat.last_bsky_url is None


async def _seed_published(session, board, *, msg_id, family, image_path=None):
    sub = make_submission(board, state=SubmissionState.PUBLISHED.value,
                          source_discord_message_id=msg_id)
    session.add(sub)
    await session.flush()
    session.add(SubmissionLink(
        submission_id=sub.id, order_index=0,
        raw_url=f"https://example.com/{msg_id}", canonical_url=f"https://example.com/{msg_id}",
        domain_family=family, resolved_image_path=image_path,
    ))
    collection = "app.bsky.feed.repost" if family == "bluesky" else "app.bsky.feed.post"
    session.add(PublishAttempt(
        submission_id=sub.id, success=True, error=None,
        at_uri=f"at://did:plc:x/{collection}/{msg_id}",
        bsky_url=None if family == "bluesky" else f"https://bsky.app/profile/x/post/{msg_id}",
        attempted_at=datetime(2026, 7, 1, 12, 0, msg_id % 60),
    ))
    return sub


async def test_recent_publishes_post_type_classification(session, board):
    await _seed_published(session, board, msg_id=1, family="bluesky")
    await _seed_published(session, board, msg_id=2, family="twitter", image_path="/tmp/i.jpg")
    await _seed_published(session, board, msg_id=3, family="other")
    await session.flush()

    rows = await recent_publishes(session, _StatSettings(), limit=10)
    types = {r.post_type for r in rows}
    assert types == {"repost", "image", "link"}


async def test_board_queue_post_type_classification(session, board):
    for msg_id, family, image in ((11, "bluesky", None), (12, "twitter", "/tmp/i.jpg"), (13, "other", None)):
        sub = make_submission(board, state=SubmissionState.QUEUED.value,
                              source_discord_message_id=msg_id)
        session.add(sub)
        await session.flush()
        session.add(SubmissionLink(
            submission_id=sub.id, order_index=0,
            raw_url=f"https://example.com/{msg_id}", canonical_url=f"https://example.com/{msg_id}",
            domain_family=family, resolved_image_path=image,
        ))
    await session.flush()

    _, items = await board_queue(session, board.name, _StatSettings())
    types = {i.post_type for i in items}
    assert types == {"repost", "image", "link"}


# ---------------------------------------------------------------------------
# replies: image_not_found
# ---------------------------------------------------------------------------


def test_image_not_found_message():
    out = replies.image_not_found()
    assert "no image attached" in out
    assert "reply again" in out


# ---------------------------------------------------------------------------
# service: naive updated_at normalization in archive rescheduling
# ---------------------------------------------------------------------------


async def _queued_with_naive_updated_at(session, board, *, msg_id, opt_out_msg=None):
    sub = make_submission(board, state=SubmissionState.QUEUED.value,
                          source_discord_message_id=msg_id, thread_id=7777)
    if opt_out_msg is not None:
        sub.playlist_opt_out_message_id = opt_out_msg
    session.add(sub)
    await session.flush()
    sub.updated_at = datetime(2026, 7, 1, 12, 0, 0)  # naive, as SQLite returns
    return sub


def _open_thread():
    thread = MagicMock(spec=discord.Thread)
    thread.archived = False
    thread.send = AsyncMock()
    return thread


async def test_playlist_opt_out_reschedule_normalizes_naive_updated_at(session, board):
    sub = await _queued_with_naive_updated_at(session, board, msg_id=21, opt_out_msg=555)
    channel = MagicMock()

    with patch("bot.discord_ingest.service._resolve_thread_by_id",
               new=AsyncMock(return_value=_open_thread())), \
         patch("bot.discord_ingest.service._fire_and_forget") as mock_fire:
        await handle_playlist_opt_out(
            session, message_id=555, user_id=sub.author_id, member=None,
            channel=channel, settings=MagicMock(), yt_client=None,
        )

    mock_fire.assert_called_once()  # naive updated_at must not crash the reschedule


async def test_playlist_skip_button_reschedule_normalizes_naive_updated_at(session, board):
    sub = await _queued_with_naive_updated_at(session, board, msg_id=22)
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = MagicMock()
    interaction.user.id = sub.author_id
    interaction.channel = MagicMock()
    interaction.message = MagicMock()
    interaction.message.edit = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()

    with patch("bot.discord_ingest.service._resolve_thread_by_id",
               new=AsyncMock(return_value=_open_thread())), \
         patch("bot.discord_ingest.service._fire_and_forget") as mock_fire:
        await handle_playlist_skip_button(session, interaction, sub.id, MagicMock(), yt_client=None)

    mock_fire.assert_called_once()
