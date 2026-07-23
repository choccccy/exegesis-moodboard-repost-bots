"""Tests for _attempt_publish edge branches, _build_post_preview, and _transcode_video.

Complements test_integration_scheduler.py and test_e2e_publish.py, which already
cover the no-handle failure, duplicate detection, null-canonical handling,
suppression cascade, and the plain success/failure paths.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select

from bot.config import BoardConfig
from bot.discord_ingest import replies
from bot.discord_ingest.service import (
    _build_post_preview,
    _transcode_video,
    publish_queued_submission,
)
from bot.models import Attachment, PublishAttempt, SubmissionLink
from bot.publish import PublishResult
from bot.state import GraphicStatus, PublishOutcome, SubmissionState

from conftest import MockDest, make_submission

QUEUED = SubmissionState.QUEUED.value


def _settings(board, *, password="app-password"):
    cfg = BoardConfig(
        name=board.name,
        discord_guild_id=board.discord_guild_id,
        discord_channel_id=board.discord_channel_id,
        bluesky_handle=f"{board.name}.exegesis.space",
        tags=[],
    )
    s = MagicMock()
    s.board_for_channel.return_value = cfg
    s.bsky_password_for.return_value = password
    return s


class _RaisingDest:
    """Notifier whose send() always fails but archive() still records."""

    def __init__(self):
        self.archived: list[str] = []

    async def send(self, content=None, **kwargs):
        raise RuntimeError("discord send failed")

    async def archive(self, notice: str) -> None:
        self.archived.append(notice)


async def _add_link(session, submission_id, url):
    link = SubmissionLink(
        submission_id=submission_id,
        order_index=0,
        raw_url=url,
        canonical_url=url,
        domain_family="other",
    )
    session.add(link)
    await session.flush()
    return link


_OK = PublishResult(
    success=True,
    at_uri="at://did:plc:new/app.bsky.feed.post/new",
    at_cid="newcid",
    bsky_url="https://bsky.app/profile/robots.exegesis.space/post/new",
)


# ---------------------------------------------------------------------------
# _attempt_publish branches
# ---------------------------------------------------------------------------


async def test_no_password_configured_fails(session, board, bind_publish_scopes):
    settings = _settings(board, password=None)
    sub = make_submission(board, state=QUEUED)
    session.add(sub)
    await session.flush()
    dest = MockDest()

    with patch("bot.publish.publish_submission", new_callable=AsyncMock) as mock_pub:
        result = await publish_queued_submission(settings, sub.id, dest)

    assert result is PublishOutcome.FAILED
    mock_pub.assert_not_awaited()
    assert sub.state == SubmissionState.PUBLISH_FAILED.value
    attempt = await session.scalar(select(PublishAttempt).where(PublishAttempt.submission_id == sub.id))
    assert attempt.success is False
    assert "no app password" in attempt.error
    assert any("failed" in m.lower() for m in dest.sent)


async def test_deferred_when_parent_butterflied_but_unpublished(session, board, bind_publish_scopes):
    settings = _settings(board)
    parent = make_submission(board, state=QUEUED, source_discord_message_id=100)
    session.add(parent)
    await session.flush()
    child = make_submission(
        board, state=QUEUED, source_discord_message_id=101,
        reply_to_discord_message_id=100,
    )
    session.add(child)
    await session.flush()

    with patch("bot.publish.publish_submission", new_callable=AsyncMock) as mock_pub:
        result = await publish_queued_submission(settings, child.id, MockDest())

    assert result is PublishOutcome.DEFERRED
    mock_pub.assert_not_awaited()
    assert child.state == QUEUED, "deferred submission stays queued for a later tick"
    attempt = await session.scalar(select(PublishAttempt).where(PublishAttempt.submission_id == child.id))
    assert attempt is None


async def test_reply_ref_passed_when_parent_published(session, board, bind_publish_scopes):
    settings = _settings(board)
    parent = make_submission(board, state=SubmissionState.PUBLISHED.value, source_discord_message_id=200)
    session.add(parent)
    await session.flush()
    session.add(PublishAttempt(
        submission_id=parent.id, success=True,
        at_uri="at://did/parent", at_cid="parentcid",
        bsky_url="https://bsky.app/parent",
    ))
    child = make_submission(
        board, state=QUEUED, source_discord_message_id=201,
        reply_to_discord_message_id=200,
    )
    session.add(child)
    await session.flush()
    await _add_link(session, child.id, "https://example.com/reply-content")

    with patch("bot.publish.publish_submission", new_callable=AsyncMock, return_value=_OK) as mock_pub:
        result = await publish_queued_submission(settings, child.id, MockDest())

    assert result is PublishOutcome.PUBLISHED
    kwargs = mock_pub.call_args.kwargs
    assert kwargs["reply_parent_uri"] == "at://did/parent"
    assert kwargs["reply_parent_cid"] == "parentcid"
    # no explicit root recorded on the parent attempt: parent becomes the root
    assert kwargs["reply_root_uri"] == "at://did/parent"
    assert kwargs["reply_root_cid"] == "parentcid"


async def test_duplicate_notice_send_failure_still_cleans_up(session, board, bind_publish_scopes):
    settings = _settings(board)
    prior = make_submission(board, state=SubmissionState.PUBLISHED.value, source_discord_message_id=300)
    session.add(prior)
    await session.flush()
    await _add_link(session, prior.id, "https://example.com/dup")
    session.add(PublishAttempt(
        submission_id=prior.id, success=True, error=None,
        at_uri="at://did/old", at_cid="oldcid", bsky_url="https://bsky.app/old",
    ))
    dup = make_submission(board, state=QUEUED, source_discord_message_id=301)
    session.add(dup)
    await session.flush()
    await _add_link(session, dup.id, "https://example.com/dup")

    dest = _RaisingDest()
    with patch("bot.publish.publish_submission", new_callable=AsyncMock) as mock_pub:
        result = await publish_queued_submission(settings, dup.id, dest)

    assert result is PublishOutcome.DUPLICATE
    mock_pub.assert_not_awaited()
    assert dup.state == SubmissionState.PUBLISHED.value
    assert dest.archived, "thread must be archived even when the notice send fails"


async def test_published_notice_send_failure_still_published_and_archived(session, board, bind_publish_scopes):
    settings = _settings(board)
    sub = make_submission(board, state=QUEUED)
    session.add(sub)
    await session.flush()
    await _add_link(session, sub.id, "https://example.com/fresh")

    dest = _RaisingDest()
    with patch("bot.publish.publish_submission", new_callable=AsyncMock, return_value=_OK):
        result = await publish_queued_submission(settings, sub.id, dest)

    assert result is PublishOutcome.PUBLISHED
    assert sub.state == SubmissionState.PUBLISHED.value
    assert dest.archived, "archive must run even when the published notice fails"
    attempt = await session.scalar(select(PublishAttempt).where(PublishAttempt.submission_id == sub.id))
    assert attempt.success is True and attempt.error is None


async def test_repost_result_sends_reposted_notice(session, board, bind_publish_scopes):
    settings = _settings(board)
    sub = make_submission(board, state=QUEUED)
    session.add(sub)
    await session.flush()
    await _add_link(session, sub.id, "https://bsky.app/profile/x/post/y")

    repost = PublishResult(
        success=True, at_uri="at://did/repost", at_cid="cid",
        bsky_url="https://bsky.app/repost", is_repost=True,
    )
    dest = MockDest()
    with patch("bot.publish.publish_submission", new_callable=AsyncMock, return_value=repost):
        result = await publish_queued_submission(settings, sub.id, dest)

    assert result is PublishOutcome.PUBLISHED
    assert replies.reposted_notice("https://bsky.app/repost") in dest.sent
    assert replies.published_notice("https://bsky.app/repost") not in dest.sent


async def test_failed_notice_send_failure_swallowed(session, board, bind_publish_scopes):
    settings = _settings(board)
    sub = make_submission(board, state=QUEUED)
    session.add(sub)
    await session.flush()

    fail = PublishResult(success=False, error="rate limited")
    dest = _RaisingDest()
    with patch("bot.publish.publish_submission", new_callable=AsyncMock, return_value=fail):
        result = await publish_queued_submission(settings, sub.id, dest)

    assert result is PublishOutcome.FAILED
    assert sub.state == SubmissionState.PUBLISH_FAILED.value


# ---------------------------------------------------------------------------
# _build_post_preview
# ---------------------------------------------------------------------------


def _image_att(sub_id, *, filename="robot.jpg", alt="a chrome robot"):
    return Attachment(
        submission_id=sub_id,
        discord_attachment_id=1,
        filename=filename,
        discord_url=f"https://cdn.discord.com/{filename}",
        mime="image/jpeg",
        is_image=True,
        is_video=False,
        alt_text_status="provided",
        alt_text_body=alt,
    )


async def test_preview_external_kind_with_nsfw_and_graphic_labels(session, board):
    board.nsfw = True
    sub = make_submission(board, graphic_status=GraphicStatus.GRAPHIC.value)
    session.add(sub)
    await session.flush()
    link = SubmissionLink(
        submission_id=sub.id, order_index=0,
        raw_url="https://example.com/a", canonical_url="https://example.com/a",
        domain_family="other", resolved_title="A Post",
        resolved_image_path="/tmp/thumb.jpg", resolved_via="opengraph",
    )

    preview = await _build_post_preview(session, sub, [], [link])

    assert preview.kind == "external"
    assert preview.labels == ["sexual", "graphic-media"]
    assert preview.nsfw is True
    assert preview.image_satisfied is True
    assert "opengraph" in preview.image_source
    assert preview.embed_has_thumb is True
    assert preview.title == "A Post"
    assert preview.board_name == board.name


async def test_preview_images_kind_lists_uploads(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    att = _image_att(sub.id)

    preview = await _build_post_preview(session, sub, [att], [])

    assert preview.kind == "images"
    assert preview.images == [("robot.jpg", "a chrome robot")]
    assert preview.labels == []
    assert preview.image_satisfied is True
    assert "1 uploaded image" in preview.image_source


async def test_preview_empty_kind_image_unsatisfied(session, board):
    sub = make_submission(board)
    session.add(sub)
    await session.flush()

    preview = await _build_post_preview(session, sub, [], [])

    assert preview.kind == "empty"
    assert preview.image_satisfied is False
    assert "no image" in preview.image_source


async def test_preview_reply_to_pending_for_deferred_parent(session, board):
    parent = make_submission(board, state=QUEUED, source_discord_message_id=400)
    session.add(parent)
    await session.flush()
    child = make_submission(board, source_discord_message_id=401, reply_to_discord_message_id=400)
    session.add(child)
    await session.flush()

    preview = await _build_post_preview(session, child, [], [])

    assert preview.reply_to_pending is True
    assert preview.reply_to_bsky_url is None


async def test_preview_reply_to_bsky_url_for_published_parent(session, board):
    parent = make_submission(board, state=SubmissionState.PUBLISHED.value, source_discord_message_id=500)
    session.add(parent)
    await session.flush()
    session.add(PublishAttempt(
        submission_id=parent.id, success=True,
        at_uri="at://did/parent", at_cid="pcid",
        bsky_url="https://bsky.app/profile/x/post/parent",
    ))
    child = make_submission(board, source_discord_message_id=501, reply_to_discord_message_id=500)
    session.add(child)
    await session.flush()

    preview = await _build_post_preview(session, child, [], [])

    assert preview.reply_to_pending is False
    assert preview.reply_to_bsky_url == "https://bsky.app/profile/x/post/parent"


# ---------------------------------------------------------------------------
# _transcode_video
# ---------------------------------------------------------------------------


def _fake_proc(returncode: int, stderr: bytes = b""):
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(b"", stderr))
    proc.returncode = returncode
    return proc


async def test_transcode_success_returns_new_path_and_removes_input(tmp_path):
    src = tmp_path / "clip.webm"
    src.write_bytes(b"fake video bytes")

    with patch(
        "bot.discord_ingest.service.asyncio.create_subprocess_exec",
        new_callable=AsyncMock, return_value=_fake_proc(0),
    ) as mock_exec:
        out = await _transcode_video(str(src))

    assert out == str(tmp_path / "clip_transcoded.mp4")
    assert not src.exists(), "original file is removed after a successful transcode"
    args = mock_exec.call_args.args
    assert args[0] == "ffmpeg"
    assert str(src) in args and out in args


async def test_transcode_failure_returns_original_path(tmp_path):
    src = tmp_path / "clip.webm"
    src.write_bytes(b"fake video bytes")

    with patch(
        "bot.discord_ingest.service.asyncio.create_subprocess_exec",
        new_callable=AsyncMock, return_value=_fake_proc(1, b"codec error"),
    ):
        out = await _transcode_video(str(src))

    assert out == str(src)
    assert src.exists(), "original file kept when ffmpeg fails"


async def test_transcode_missing_input_remove_error_swallowed(tmp_path):
    src = tmp_path / "ghost.webm"  # never created: os.remove will raise OSError

    with patch(
        "bot.discord_ingest.service.asyncio.create_subprocess_exec",
        new_callable=AsyncMock, return_value=_fake_proc(0),
    ):
        out = await _transcode_video(str(src))  # must not raise

    assert out == str(tmp_path / "ghost_transcoded.mp4")
