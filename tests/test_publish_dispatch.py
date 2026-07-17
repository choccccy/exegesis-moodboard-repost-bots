"""Dispatch and reply-chain tests for src/bot/publish/__init__.py.

Focuses on publish_submission's login/dispatch/bookkeeping flow, the video and
image reply chains, and the remaining uncovered branches (blob upload paths,
resolve failures, empty-link text fallbacks). The AsyncClient constructed
inside publish_submission is replaced by patching bot.publish.AsyncClient so
nothing here touches the network. Model constructors and client method names
were checked against atproto==0.0.68.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from atproto import models
from atproto_client.models.blob_ref import BlobRef, IpldLink

from bot.config import BoardConfig
from bot.models import Attachment, Submission, SubmissionLink
from bot.publish import (
    _compress_for_bsky,
    _publish_image_reply,
    _publish_images,
    _publish_record,
    _publish_reply_post,
    _publish_reply_thread,
    _publish_video,
    _publish_video_reply,
    _resolve_bluesky_post,
    _upload_blob,
    _upload_video_blob,
    publish_submission,
)
from bot.state import GraphicStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _link(
    canonical_url: str = "https://www.artstation.com/artwork/BmBmAA",
    domain_family: str = "artstation",
    resolved_title: str = "Cool Artwork",
    resolved_description: str = "A great piece",
    resolved_image_path: str | None = None,
    source_at_uri: str | None = None,
) -> SubmissionLink:
    link = MagicMock(spec=SubmissionLink)
    link.canonical_url = canonical_url
    link.domain_family = domain_family
    link.resolved_title = resolved_title
    link.resolved_description = resolved_description
    link.resolved_image_path = resolved_image_path
    link.source_at_uri = source_at_uri
    return link


def _image_attachment(
    local_path: str | None = "/data/attachments/1/img.jpg",
    alt_text_body: str = "a robot",
) -> Attachment:
    att = MagicMock(spec=Attachment)
    att.id = 7
    att.is_image = True
    att.is_video = False
    att.local_path = local_path
    att.alt_text_body = alt_text_body
    return att


def _video_attachment(
    local_path: str | None = "/data/attachments/1/clip.mp4",
    alt_text_body: str = "a video",
    width: int | None = 640,
    height: int | None = 480,
    att_id: int = 99,
) -> Attachment:
    att = MagicMock(spec=Attachment)
    att.id = att_id
    att.is_image = False
    att.is_video = True
    att.local_path = local_path
    att.alt_text_body = alt_text_body
    att.width = width
    att.height = height
    return att


def _submission(graphic_status: str = GraphicStatus.NOT_GRAPHIC.value) -> Submission:
    sub = MagicMock(spec=Submission)
    sub.id = 1
    sub.graphic_status = graphic_status
    return sub


def _board(
    nsfw: bool = False,
    tags: list[str] | None = None,
    bluesky_handle: str | None = "robots.exegesis.space",
) -> BoardConfig:
    return BoardConfig(
        name="robot-posting",
        discord_guild_id=123,
        discord_channel_id=456,
        nsfw=nsfw,
        bluesky_handle=bluesky_handle,
        tags=tags if tags is not None else [],
    )


def _fake_blob(link: str = "bafyreitest000") -> BlobRef:
    return BlobRef(mime_type="image/jpeg", size=100, ref=IpldLink(link=link))


def _fake_video_blob() -> BlobRef:
    return BlobRef(mime_type="video/mp4", size=1000, ref=IpldLink(link="bafyreivideo000"))


def _create_resp(rkey: str, did: str = "did:plc:testdid000") -> MagicMock:
    resp = MagicMock()
    resp.uri = f"at://{did}/app.bsky.feed.post/{rkey}"
    resp.cid = f"cid-{rkey}"
    return resp


def _fake_client(did: str = "did:plc:testdid000") -> MagicMock:
    """Async-capable AsyncClient stand-in covering everything publish uses."""
    client = MagicMock()
    client.me = MagicMock()
    client.me.did = did

    client.login = AsyncMock()
    client.com.atproto.repo.create_record = AsyncMock(return_value=_create_resp("abc123", did))

    upload_resp = MagicMock()
    upload_resp.blob = _fake_blob()
    client.upload_blob = AsyncMock(return_value=upload_resp)

    repost_resp = MagicMock()
    repost_resp.uri = f"at://{did}/app.bsky.feed.repost/rp123"
    repost_resp.cid = "bafyreitest_repost"
    client.repost = AsyncMock(return_value=repost_resp)
    client.like = AsyncMock()

    resolve_resp = MagicMock()
    resolve_resp.did = "did:plc:targetdid000"
    client.resolve_handle = AsyncMock(return_value=resolve_resp)

    target_post = MagicMock()
    target_post.cid = "bafyreitarget000"
    posts_resp = MagicMock()
    posts_resp.posts = [target_post]
    client.get_posts = AsyncMock(return_value=posts_resp)

    return client


def _patched_client(client: MagicMock):
    """Patch bot.publish.AsyncClient so publish_submission gets our fake."""
    mock_cls = MagicMock(return_value=client)
    return patch("bot.publish.AsyncClient", mock_cls)


def _record_of(call) -> models.AppBskyFeedPost.Record:
    return call[0][0].record


# ---------------------------------------------------------------------------
# publish_submission - login and preconditions
# ---------------------------------------------------------------------------

async def test_publish_submission_no_handle_fails_without_client():
    with patch("bot.publish.AsyncClient") as mock_cls:
        result = await publish_submission(
            _submission(), [_link()], [], _board(bluesky_handle=None), "pw"
        )
    assert not result.success
    assert result.error is not None and "no bluesky_handle" in result.error
    mock_cls.assert_not_called()


async def test_publish_submission_login_failure_returns_error():
    client = _fake_client()
    client.login = AsyncMock(side_effect=Exception("bad app password"))
    with _patched_client(client):
        result = await publish_submission(_submission(), [_link()], [], _board(), "pw")
    assert not result.success
    assert result.error is not None
    assert "login failed" in result.error
    assert "bad app password" in result.error
    client.com.atproto.repo.create_record.assert_not_called()


async def test_publish_submission_login_called_with_handle_and_password():
    client = _fake_client()
    with _patched_client(client):
        await publish_submission(_submission(), [_link()], [], _board(), "app-password")
    client.login.assert_awaited_once_with("robots.exegesis.space", "app-password")


# ---------------------------------------------------------------------------
# publish_submission - dispatch per kind
# ---------------------------------------------------------------------------

async def test_publish_submission_record_kind_reposts_and_likes_original():
    client = _fake_client()
    link = _link(
        canonical_url="https://bsky.app/profile/did:plc:targetdid000/post/xyz789",
        domain_family="bluesky",
    )
    with _patched_client(client):
        result = await publish_submission(_submission(), [link], [], _board(), "pw")
    assert result.success
    assert result.is_repost
    client.repost.assert_awaited_once_with(
        "at://did:plc:targetdid000/app.bsky.feed.post/xyz789", "bafyreitarget000"
    )
    # Like targets the original post, not the repost record.
    client.like.assert_awaited_once_with(
        "at://did:plc:targetdid000/app.bsky.feed.post/xyz789", "bafyreitarget000"
    )
    assert result.bsky_url == link.canonical_url


async def test_publish_submission_external_kind_creates_external_embed():
    client = _fake_client()
    with _patched_client(client):
        result = await publish_submission(_submission(), [_link()], [], _board(), "pw")
    assert result.success
    record = _record_of(client.com.atproto.repo.create_record.call_args)
    assert isinstance(record.embed, models.AppBskyEmbedExternal.Main)


async def test_publish_submission_images_kind_creates_images_embed():
    client = _fake_client()
    with (
        _patched_client(client),
        patch("bot.publish._upload_blob", new=AsyncMock(return_value=(_fake_blob(), None))),
    ):
        result = await publish_submission(
            _submission(), [_link()], [_image_attachment()], _board(), "pw"
        )
    assert result.success
    record = _record_of(client.com.atproto.repo.create_record.call_args)
    assert isinstance(record.embed, models.AppBskyEmbedImages.Main)


async def test_publish_submission_video_kind_creates_video_embed():
    client = _fake_client()
    with (
        _patched_client(client),
        patch("bot.publish._upload_video_blob", new=AsyncMock(return_value=(_fake_video_blob(), None))),
    ):
        result = await publish_submission(
            _submission(), [_link()], [_video_attachment()], _board(), "pw"
        )
    assert result.success
    record = _record_of(client.com.atproto.repo.create_record.call_args)
    assert isinstance(record.embed, models.AppBskyEmbedVideo.Main)


async def test_publish_submission_empty_kind_returns_failure():
    client = _fake_client()
    with _patched_client(client):
        result = await publish_submission(_submission(), [], [], _board(), "pw")
    assert not result.success
    assert result.error is not None and "unsupported embed kind: empty" in result.error


async def test_publish_submission_publisher_exception_returns_failure():
    client = _fake_client()
    client.com.atproto.repo.create_record = AsyncMock(side_effect=Exception("boom 500"))
    with _patched_client(client):
        result = await publish_submission(_submission(), [_link()], [], _board(), "pw")
    assert not result.success
    assert result.error == "boom 500"
    client.like.assert_not_awaited()


async def test_publish_submission_failed_sub_publish_skips_bookkeeping():
    # Images kind where no image can be uploaded: failure result, no like,
    # no root/url bookkeeping.
    client = _fake_client()
    with _patched_client(client):
        result = await publish_submission(
            _submission(), [_link()], [_image_attachment(local_path=None)], _board(), "pw"
        )
    assert not result.success
    assert result.bsky_root_uri is None
    assert result.bsky_url is None
    client.like.assert_not_awaited()


# ---------------------------------------------------------------------------
# publish_submission - post-success bookkeeping
# ---------------------------------------------------------------------------

async def test_publish_submission_like_failure_still_succeeds_with_url():
    client = _fake_client()
    client.like = AsyncMock(side_effect=Exception("rate limited"))
    with _patched_client(client):
        result = await publish_submission(_submission(), [_link()], [], _board(), "pw")
    assert result.success
    assert result.bsky_url == "https://bsky.app/profile/robots.exegesis.space/post/abc123"
    assert result.bsky_root_uri == result.at_uri


async def test_publish_submission_reply_kwargs_thread_into_post():
    client = _fake_client()
    with _patched_client(client):
        result = await publish_submission(
            _submission(), [_link()], [], _board(), "pw",
            reply_parent_uri="at://x/app.bsky.feed.post/parent1",
            reply_parent_cid="cid-parent1",
            reply_root_uri="at://x/app.bsky.feed.post/root1",
            reply_root_cid="cid-root1",
        )
    assert result.success
    record = _record_of(client.com.atproto.repo.create_record.call_args)
    assert record.reply is not None
    assert record.reply.parent.uri == "at://x/app.bsky.feed.post/parent1"
    assert record.reply.parent.cid == "cid-parent1"
    assert record.reply.root.uri == "at://x/app.bsky.feed.post/root1"
    # The result inherits the supplied root, not its own post.
    assert result.bsky_root_uri == "at://x/app.bsky.feed.post/root1"
    assert result.bsky_root_cid == "cid-root1"


async def test_publish_submission_partial_reply_kwargs_ignored():
    # Missing root cid means no ReplyRef is built at all.
    client = _fake_client()
    with _patched_client(client):
        result = await publish_submission(
            _submission(), [_link()], [], _board(), "pw",
            reply_parent_uri="at://x/app.bsky.feed.post/parent1",
            reply_parent_cid="cid-parent1",
            reply_root_uri="at://x/app.bsky.feed.post/root1",
        )
    assert result.success
    record = _record_of(client.com.atproto.repo.create_record.call_args)
    assert record.reply is None
    # bsky_root_uri still inherits the passed root uri; the cid falls back
    # to the new post's own cid.
    assert result.bsky_root_uri == "at://x/app.bsky.feed.post/root1"
    assert result.bsky_root_cid == result.at_cid


async def test_publish_submission_own_post_becomes_root_when_not_a_reply():
    client = _fake_client()
    with _patched_client(client):
        result = await publish_submission(_submission(), [_link()], [], _board(), "pw")
    assert result.bsky_root_uri == result.at_uri
    assert result.bsky_root_cid == result.at_cid


# ---------------------------------------------------------------------------
# publish_submission - video/image reply chains
# ---------------------------------------------------------------------------

async def test_publish_submission_video_chain_threads_replies_in_order():
    client = _fake_client()
    client.com.atproto.repo.create_record = AsyncMock(
        side_effect=[_create_resp("main"), _create_resp("vreply"), _create_resp("ireply")]
    )
    atts = [
        _video_attachment(att_id=1),
        _video_attachment(att_id=2, alt_text_body="second video"),
        _image_attachment(alt_text_body="bonus image"),
    ]
    with (
        _patched_client(client),
        patch("bot.publish._upload_video_blob", new=AsyncMock(return_value=(_fake_video_blob(), None))),
        patch("bot.publish._upload_blob", new=AsyncMock(return_value=(_fake_blob(), None))),
    ):
        result = await publish_submission(_submission(), [_link()], atts, _board(), "pw")

    assert result.success
    calls = client.com.atproto.repo.create_record.call_args_list
    assert len(calls) == 3

    main_record = _record_of(calls[0])
    assert isinstance(main_record.embed, models.AppBskyEmbedVideo.Main)
    assert main_record.reply is None

    video_reply = _record_of(calls[1])
    assert isinstance(video_reply.embed, models.AppBskyEmbedVideo.Main)
    assert video_reply.embed.alt == "second video"
    assert video_reply.reply.root.uri == "at://did:plc:testdid000/app.bsky.feed.post/main"
    assert video_reply.reply.parent.uri == "at://did:plc:testdid000/app.bsky.feed.post/main"

    image_reply = _record_of(calls[2])
    assert isinstance(image_reply.embed, models.AppBskyEmbedImages.Main)
    assert image_reply.reply.root.uri == "at://did:plc:testdid000/app.bsky.feed.post/main"
    assert image_reply.reply.parent.uri == "at://did:plc:testdid000/app.bsky.feed.post/vreply"
    assert image_reply.reply.parent.cid == "cid-vreply"

    # Root and every reply is self-liked (best-effort), one like per created post.
    assert client.like.await_count == 3
    liked = {call.args[0] for call in client.like.await_args_list}
    assert liked == {
        "at://did:plc:testdid000/app.bsky.feed.post/main",
        "at://did:plc:testdid000/app.bsky.feed.post/vreply",
        "at://did:plc:testdid000/app.bsky.feed.post/ireply",
    }


async def test_publish_submission_video_reply_failure_keeps_main_success():
    # Second video's upload fails inside _publish_video_reply: the loop breaks
    # (third video is never attempted) but the main result stays successful.
    client = _fake_client()
    client.com.atproto.repo.create_record = AsyncMock(
        side_effect=[_create_resp("main"), _create_resp("ireply")]
    )
    atts = [
        _video_attachment(att_id=1),
        _video_attachment(att_id=2),
        _video_attachment(att_id=3),
        _image_attachment(),
    ]
    upload_video = AsyncMock(
        side_effect=[(_fake_video_blob(), None), (None, "Exception: upload broke")]
    )
    with (
        _patched_client(client),
        patch("bot.publish._upload_video_blob", new=upload_video),
        patch("bot.publish._upload_blob", new=AsyncMock(return_value=(_fake_blob(), None))),
    ):
        result = await publish_submission(_submission(), [_link()], atts, _board(), "pw")

    assert result.success
    # Only the main video upload plus the one failed reply upload happened.
    assert upload_video.await_count == 2
    calls = client.com.atproto.repo.create_record.call_args_list
    assert len(calls) == 2
    # The image reply still goes out, chained off the main post since the
    # failed video reply never advanced the parent.
    image_reply = _record_of(calls[1])
    assert isinstance(image_reply.embed, models.AppBskyEmbedImages.Main)
    assert image_reply.reply.parent.uri == "at://did:plc:testdid000/app.bsky.feed.post/main"


async def test_publish_submission_image_reply_failure_keeps_main_success():
    client = _fake_client()
    atts = [_video_attachment(), _image_attachment(local_path=None)]
    with (
        _patched_client(client),
        patch("bot.publish._upload_video_blob", new=AsyncMock(return_value=(_fake_video_blob(), None))),
    ):
        result = await publish_submission(_submission(), [_link()], atts, _board(), "pw")
    assert result.success
    # Only the main video post; the image reply raised and was swallowed.
    assert client.com.atproto.repo.create_record.await_count == 1


async def test_publish_submission_link_replies_chain_after_media_replies():
    client = _fake_client()
    client.com.atproto.repo.create_record = AsyncMock(
        side_effect=[_create_resp("main"), _create_resp("vreply"), _create_resp("lreply")]
    )
    links = [
        _link(canonical_url="https://www.artstation.com/artwork/BmBmAA"),
        _link(canonical_url="https://www.artstation.com/artwork/extra"),
    ]
    atts = [_video_attachment(att_id=1), _video_attachment(att_id=2)]
    with (
        _patched_client(client),
        patch("bot.publish._upload_video_blob", new=AsyncMock(return_value=(_fake_video_blob(), None))),
    ):
        result = await publish_submission(_submission(), links, atts, _board(), "pw")

    assert result.success
    calls = client.com.atproto.repo.create_record.call_args_list
    assert len(calls) == 3
    link_reply = _record_of(calls[2])
    assert "artstation.com/artwork/extra" in link_reply.text
    # Link replies now carry the same external-embed card as the root post.
    assert isinstance(link_reply.embed, models.AppBskyEmbedExternal.Main)
    assert link_reply.embed.external.uri == "https://www.artstation.com/artwork/extra"
    assert link_reply.reply.root.uri == "at://did:plc:testdid000/app.bsky.feed.post/main"
    # The link reply chains off the last media reply, not the root.
    assert link_reply.reply.parent.uri == "at://did:plc:testdid000/app.bsky.feed.post/vreply"


async def test_reply_post_builds_external_card_with_thumbnail():
    """Regression: link replies used to post with embed=None (bare text link).
    They must now build an external-embed card and upload the resolved thumbnail."""
    client = _fake_client()
    link = _link(
        canonical_url="https://www.artstation.com/artwork/extra",
        resolved_title="Extra Piece",
        resolved_description="another great one",
        resolved_image_path="/data/thumb_extra.jpg",
    )
    with (
        _patched_client(client),
        patch("bot.publish._upload_blob", new=AsyncMock(return_value=(_fake_blob(), None))) as mock_upload,
    ):
        uri, cid = await _publish_reply_post(
            client, link, labels=None, tags=[],
            root_uri="at://x/app.bsky.feed.post/root", root_cid="cid-root",
            parent_uri="at://x/app.bsky.feed.post/root", parent_cid="cid-root",
        )

    mock_upload.assert_awaited_once_with(client, "/data/thumb_extra.jpg")
    record = _record_of(client.com.atproto.repo.create_record.call_args)
    assert isinstance(record.embed, models.AppBskyEmbedExternal.Main)
    assert record.embed.external.title == "Extra Piece"
    assert record.embed.external.description == "another great one"
    assert record.embed.external.thumb is not None
    # The reply is self-liked.
    client.like.assert_awaited_once_with(uri, cid)


async def test_reply_post_external_card_without_thumbnail():
    """A link with no resolved thumbnail still gets a card, just no image."""
    client = _fake_client()
    link = _link(canonical_url="https://example.com/x", resolved_image_path=None)
    with _patched_client(client), patch("bot.publish._upload_blob", new=AsyncMock()) as mock_upload:
        await _publish_reply_post(
            client, link, labels=None, tags=[],
            root_uri="at://x/app.bsky.feed.post/root", root_cid="cid-root",
            parent_uri="at://x/app.bsky.feed.post/root", parent_cid="cid-root",
        )
    mock_upload.assert_not_awaited()
    record = _record_of(client.com.atproto.repo.create_record.call_args)
    assert isinstance(record.embed, models.AppBskyEmbedExternal.Main)
    assert record.embed.external.thumb is None


# ---------------------------------------------------------------------------
# _publish_reply_thread / _publish_reply_post edge cases
# ---------------------------------------------------------------------------

async def test_reply_thread_stops_at_first_failure():
    client = _fake_client()
    client.com.atproto.repo.create_record = AsyncMock(
        side_effect=[_create_resp("r1"), Exception("boom"), _create_resp("r3")]
    )
    links = [_link(canonical_url=f"https://example.com/{i}") for i in range(3)]
    await _publish_reply_thread(
        client, links, labels=None, tags=[],
        root_uri="at://x/app.bsky.feed.post/root", root_cid="cid-root", submission_id=1,
    )
    # First succeeds, second raises, third is never attempted.
    assert client.com.atproto.repo.create_record.await_count == 2


async def test_reply_thread_advances_parent_between_links():
    client = _fake_client()
    client.com.atproto.repo.create_record = AsyncMock(
        side_effect=[_create_resp("r1"), _create_resp("r2")]
    )
    links = [_link(canonical_url=f"https://example.com/{i}") for i in range(2)]
    await _publish_reply_thread(
        client, links, labels=None, tags=[],
        root_uri="at://x/app.bsky.feed.post/root", root_cid="cid-root", submission_id=1,
    )
    calls = client.com.atproto.repo.create_record.call_args_list
    assert _record_of(calls[0]).reply.parent.uri == "at://x/app.bsky.feed.post/root"
    assert _record_of(calls[1]).reply.parent.uri == "at://did:plc:testdid000/app.bsky.feed.post/r1"


async def test_reply_post_raises_when_no_uri_returned():
    client = _fake_client()
    resp = MagicMock()
    resp.uri = None
    resp.cid = None
    client.com.atproto.repo.create_record = AsyncMock(return_value=resp)
    with pytest.raises(RuntimeError, match="no URI/CID"):
        await _publish_reply_post(
            client, _link(), labels=None, tags=[],
            root_uri="at://x/app.bsky.feed.post/r", root_cid="c",
            parent_uri="at://x/app.bsky.feed.post/r", parent_cid="c",
        )


# ---------------------------------------------------------------------------
# _resolve_bluesky_post / _publish_record edge cases
# ---------------------------------------------------------------------------

async def test_resolve_bluesky_post_at_uri_input_raises():
    # An at:// URI is not a bsky.app URL; path splitting comes up short.
    client = _fake_client()
    with pytest.raises(IndexError):
        await _resolve_bluesky_post(
            client, "at://did:plc:targetdid000/app.bsky.feed.post/xyz789"
        )


async def test_publish_record_malformed_url_returns_failure():
    client = _fake_client()
    link = _link(canonical_url="https://bsky.app/notapost", domain_family="bluesky")
    result = await _publish_record(client, [link])
    assert not result.success
    assert result.error is not None and "could not resolve Bluesky post" in result.error
    client.repost.assert_not_awaited()


async def test_publish_record_like_failure_still_succeeds():
    client = _fake_client()
    client.like = AsyncMock(side_effect=Exception("rate limited"))
    link = _link(
        canonical_url="https://bsky.app/profile/did:plc:targetdid000/post/xyz789",
        domain_family="bluesky",
    )
    result = await _publish_record(client, [link])
    assert result.success
    assert result.is_repost


async def test_publish_record_prefers_pinned_at_uri_over_handle():
    # A pinned DID-based at:// URI must be used verbatim, never re-resolving the
    # (possibly stale) handle in canonical_url.
    client = _fake_client()
    at_uri = "at://did:plc:pinned000/app.bsky.feed.post/rk123"
    link = _link(
        canonical_url="https://bsky.app/profile/oldhandle.bsky.social/post/rk123",
        domain_family="bluesky",
        source_at_uri=at_uri,
    )
    result = await _publish_record(client, [link])
    assert result.success and result.is_repost
    client.resolve_handle.assert_not_awaited()  # handle never touched
    client.get_posts.assert_awaited_once_with([at_uri])
    client.repost.assert_awaited_once_with(at_uri, "bafyreitarget000")


async def test_publish_record_falls_back_to_handle_when_no_pinned_uri():
    # Legacy rows without a pinned URI resolve the handle live.
    client = _fake_client()
    link = _link(
        canonical_url="https://bsky.app/profile/live.bsky.social/post/rk777",
        domain_family="bluesky",
        source_at_uri=None,
    )
    result = await _publish_record(client, [link])
    assert result.success and result.is_repost
    client.resolve_handle.assert_awaited_once_with("live.bsky.social")


# ---------------------------------------------------------------------------
# _upload_blob
# ---------------------------------------------------------------------------

async def test_upload_blob_small_file_uploads_raw_bytes(tmp_path):
    f = tmp_path / "img.jpg"
    f.write_bytes(b"tiny-jpeg-bytes")
    client = _fake_client()
    blob, err = await _upload_blob(client, str(f))
    assert blob is client.upload_blob.return_value.blob
    assert err is None
    client.upload_blob.assert_awaited_once_with(b"tiny-jpeg-bytes")


async def test_upload_blob_compresses_oversize_file(tmp_path):
    f = tmp_path / "big.jpg"
    f.write_bytes(b"x" * 64)
    client = _fake_client()
    with (
        patch("bot.publish._BSKY_MAX_BLOB", 32),
        patch("bot.publish._compress_for_bsky", return_value=b"small") as compress,
    ):
        blob, err = await _upload_blob(client, str(f))
    assert blob is not None
    assert err is None
    compress.assert_called_once_with(b"x" * 64)
    client.upload_blob.assert_awaited_once_with(b"small")


async def test_upload_blob_missing_file_returns_error_detail():
    client = _fake_client()
    blob, err = await _upload_blob(client, "/nope/does-not-exist.jpg")
    assert blob is None
    assert err is not None and "FileNotFoundError" in err
    client.upload_blob.assert_not_awaited()


async def test_upload_blob_upload_exception_returns_error_detail(tmp_path):
    f = tmp_path / "img.jpg"
    f.write_bytes(b"bytes")
    client = _fake_client()
    client.upload_blob = AsyncMock(side_effect=Exception("status_code=502"))
    blob, err = await _upload_blob(client, str(f))
    assert blob is None
    assert err is not None and "status_code=502" in err


def test_compress_for_bsky_halves_resolution_when_quality_not_enough():
    import io
    import os

    from PIL import Image

    # Random noise compresses poorly, so quality steps alone cannot fit a
    # tiny patched limit and the resize loop must kick in.
    img = Image.frombytes("RGB", (256, 256), os.urandom(256 * 256 * 3))
    buf = io.BytesIO()
    img.save(buf, format="PNG")

    # Limit chosen between the 128x128 and 256x256 q=25 JPEG sizes.
    small = io.BytesIO()
    img.resize((128, 128), Image.LANCZOS).save(small, format="JPEG", quality=25, optimize=True)
    full = io.BytesIO()
    img.save(full, format="JPEG", quality=25, optimize=True)
    limit = (small.tell() + full.tell()) // 2

    with patch("bot.publish._BSKY_MAX_BLOB", limit):
        out = _compress_for_bsky(buf.getvalue())

    assert len(out) <= limit
    assert Image.open(io.BytesIO(out)).format == "JPEG"


# ---------------------------------------------------------------------------
# Video helpers - remaining branches
# ---------------------------------------------------------------------------

async def test_upload_video_blob_unreadable_file_returns_error():
    client = _fake_client()
    att = _video_attachment(local_path="/nope/missing.mp4")
    blob, err = await _upload_video_blob(client, att)
    assert blob is None
    assert err is not None and "FileNotFoundError" in err
    client.upload_blob.assert_not_awaited()


async def test_publish_video_without_video_attachment_fails():
    client = _fake_client()
    result = await _publish_video(client, [_link()], [_image_attachment()], labels=None, tags=[])
    assert not result.success
    assert result.error == "no video attachment found"


async def test_publish_video_no_links_and_no_dimensions(tmp_path):
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"fake-video")
    client = _fake_client()
    att = _video_attachment(local_path=str(f), width=None, height=None)
    result = await _publish_video(client, [], [att], labels=None, tags=["robots"])
    assert result.success
    record = _record_of(client.com.atproto.repo.create_record.call_args)
    assert record.embed.aspect_ratio is None
    # No link: source was waived, so the text carries a "source unknown" note plus tags.
    assert record.text == "source unknown #robots"


async def test_publish_images_no_links_notes_source_unknown():
    client = _fake_client()
    with patch("bot.publish._upload_blob", new=AsyncMock(return_value=(_fake_blob(), None))):
        result = await _publish_images(client, [], [_image_attachment()], labels=None, tags=[])
    assert result.success
    record = _record_of(client.com.atproto.repo.create_record.call_args)
    # No link means the source was waived; posts a "source unknown" note instead of bare media.
    assert record.text == "source unknown"


async def test_publish_images_with_source_note():
    client = _fake_client()
    with patch("bot.publish._upload_blob", new=AsyncMock(return_value=(_fake_blob(), None))):
        result = await _publish_images(
            client, [], [_image_attachment()], labels=None, tags=["robots"],
            source_note="Popular Mechanics, March 1965",
        )
    assert result.success
    record = _record_of(client.com.atproto.repo.create_record.call_args)
    # Non-URL source publishes as "source: <note>" instead of "source unknown".
    assert record.text == "source: Popular Mechanics, March 1965 #robots"


async def test_publish_video_with_source_note(tmp_path):
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"fake-video")
    client = _fake_client()
    att = _video_attachment(local_path=str(f), width=None, height=None)
    result = await _publish_video(
        client, [], [att], labels=None, tags=[], source_note="US National Archives",
    )
    assert result.success
    record = _record_of(client.com.atproto.repo.create_record.call_args)
    assert record.text == "source: US National Archives"


async def test_publish_video_reply_upload_failure_raises():
    client = _fake_client()
    att = _video_attachment(local_path=None, att_id=42)
    with pytest.raises(RuntimeError) as excinfo:
        await _publish_video_reply(
            client, att, [_link()], labels=None, tags=[],
            root_uri="at://x/app.bsky.feed.post/r", root_cid="c",
            parent_uri="at://x/app.bsky.feed.post/r", parent_cid="c",
        )
    assert "video upload failed for attachment 42" in str(excinfo.value)
    assert "no local file" in str(excinfo.value)


async def test_publish_video_reply_no_links_and_no_dimensions(tmp_path):
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"fake-video")
    client = _fake_client()
    att = _video_attachment(local_path=str(f), width=None, height=None)
    uri, cid = await _publish_video_reply(
        client, att, [], labels=None, tags=[],
        root_uri="at://x/app.bsky.feed.post/r", root_cid="c",
        parent_uri="at://x/app.bsky.feed.post/p", parent_cid="pc",
    )
    record = _record_of(client.com.atproto.repo.create_record.call_args)
    assert record.embed.aspect_ratio is None
    assert record.text == ""
    assert record.reply.root.uri == "at://x/app.bsky.feed.post/r"
    assert record.reply.parent.uri == "at://x/app.bsky.feed.post/p"
    assert uri == "at://did:plc:testdid000/app.bsky.feed.post/abc123"
    assert cid == "cid-abc123"


async def test_publish_image_reply_no_uploadable_images_raises():
    client = _fake_client()
    atts = [_image_attachment(local_path=None)]
    with pytest.raises(RuntimeError) as excinfo:
        await _publish_image_reply(
            client, atts, [_link()], labels=None, tags=[],
            root_uri="at://x/app.bsky.feed.post/r", root_cid="c",
            parent_uri="at://x/app.bsky.feed.post/r", parent_cid="c",
        )
    assert "no images could be uploaded for image reply" in str(excinfo.value)
    assert "no local file" in str(excinfo.value)


async def test_publish_image_reply_caps_at_four_images():
    client = _fake_client()
    atts = [_image_attachment(alt_text_body=f"img {i}") for i in range(6)]
    with patch("bot.publish._upload_blob", new=AsyncMock(return_value=(_fake_blob(), None))) as upload:
        await _publish_image_reply(
            client, atts, [], labels=None, tags=[],
            root_uri="at://x/app.bsky.feed.post/r", root_cid="c",
            parent_uri="at://x/app.bsky.feed.post/r", parent_cid="c",
        )
    assert upload.await_count == 4
    record = _record_of(client.com.atproto.repo.create_record.call_args)
    assert len(record.embed.images) == 4
    assert record.text == ""


async def test_publish_video_reply_no_uri_raises(tmp_path):
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"fake-video")
    client = _fake_client()
    bad_resp = MagicMock()
    bad_resp.uri = None
    bad_resp.cid = None
    client.com.atproto.repo.create_record = AsyncMock(return_value=bad_resp)
    att = _video_attachment(local_path=str(f))
    with pytest.raises(RuntimeError, match="video reply post returned no URI/CID"):
        await _publish_video_reply(
            client, att, [_link()], labels=None, tags=[],
            root_uri="at://x/app.bsky.feed.post/r", root_cid="c",
            parent_uri="at://x/app.bsky.feed.post/p", parent_cid="pc",
        )


async def test_publish_image_reply_no_uri_raises():
    client = _fake_client()
    bad_resp = MagicMock()
    bad_resp.uri = None
    bad_resp.cid = None
    client.com.atproto.repo.create_record = AsyncMock(return_value=bad_resp)
    with patch("bot.publish._upload_blob", new=AsyncMock(return_value=(_fake_blob(), None))):
        with pytest.raises(RuntimeError, match="image reply post returned no URI/CID"):
            await _publish_image_reply(
                client, [_image_attachment()], [_link()], labels=None, tags=[],
                root_uri="at://x/app.bsky.feed.post/r", root_cid="c",
                parent_uri="at://x/app.bsky.feed.post/p", parent_cid="pc",
            )


async def test_publish_image_reply_collects_upload_error_detail():
    client = _fake_client()
    with patch("bot.publish._upload_blob", new=AsyncMock(return_value=(None, "Exception: cdn down"))):
        with pytest.raises(RuntimeError) as excinfo:
            await _publish_image_reply(
                client, [_image_attachment()], [_link()], labels=None, tags=[],
                root_uri="at://x/app.bsky.feed.post/r", root_cid="c",
                parent_uri="at://x/app.bsky.feed.post/p", parent_cid="pc",
            )
    assert "cdn down" in str(excinfo.value)
