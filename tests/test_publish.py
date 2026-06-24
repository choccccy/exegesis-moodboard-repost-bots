"""Tests for src/bot/publish/__init__.py

Pure functions are tested directly. Network-dependent functions use AsyncMock
so we verify record construction without hitting Bluesky. All field names and
model constructors were verified against atproto==0.0.68 before writing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from atproto import models
from atproto_client.models.blob_ref import BlobRef, IpldLink

from bot.config import BoardConfig
from bot.models import Attachment, Submission, SubmissionLink
from bot.publish import (
    PublishResult,
    _append_tags,
    _build_labels,
    _create_post,
    _determine_kind,
    _post_text_and_facets,
    _publish_external,
    _publish_images,
    _publish_record,
    _publish_reply_post,
    _resolve_bluesky_post,
    at_uri_to_url,
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
) -> SubmissionLink:
    link = MagicMock(spec=SubmissionLink)
    link.canonical_url = canonical_url
    link.domain_family = domain_family
    link.resolved_title = resolved_title
    link.resolved_description = resolved_description
    link.resolved_image_path = resolved_image_path
    return link


def _attachment(
    is_image: bool = True,
    local_path: str | None = "/data/attachments/1/img.jpg",
    alt_text_body: str = "a robot",
) -> Attachment:
    att = MagicMock(spec=Attachment)
    att.is_image = is_image
    att.local_path = local_path
    att.alt_text_body = alt_text_body
    return att


def _submission(graphic_status: str = GraphicStatus.NOT_GRAPHIC.value) -> Submission:
    sub = MagicMock(spec=Submission)
    sub.id = 1
    sub.graphic_status = graphic_status
    return sub


def _board(nsfw: bool = False, tags: list[str] | None = None) -> BoardConfig:
    return BoardConfig(
        name="robot-posting",
        discord_guild_id=123,
        discord_channel_id=456,
        nsfw=nsfw,
        bluesky_handle="robots.exegesis.space",
        tags=tags if tags is not None else ["robot-posting"],
    )


def _fake_blob() -> BlobRef:
    """A real BlobRef that passes atproto pydantic validation."""
    return BlobRef(mime_type="image/jpeg", size=100, ref=IpldLink(link="bafyreitest000"))


def _mock_client(did: str = "did:plc:testdid000") -> MagicMock:
    """Minimal atproto AsyncClient mock with create_record, repost, resolve_handle, get_posts."""
    client = MagicMock()
    client.me = MagicMock()
    client.me.did = did

    create_resp = MagicMock()
    create_resp.uri = f"at://{did}/app.bsky.feed.post/abc123"
    create_resp.cid = "bafyreitest000"
    client.com.atproto.repo.create_record = AsyncMock(return_value=create_resp)

    repost_resp = MagicMock()
    repost_resp.uri = f"at://{did}/app.bsky.feed.repost/rp123"
    repost_resp.cid = "bafyreitest_repost"
    client.repost = AsyncMock(return_value=repost_resp)

    resolve_resp = MagicMock()
    resolve_resp.did = "did:plc:targetdid000"
    client.resolve_handle = AsyncMock(return_value=resolve_resp)

    target_post = MagicMock()
    target_post.uri = "at://did:plc:targetdid000/app.bsky.feed.post/xyz789"
    target_post.cid = "bafyreitarget000"
    posts_resp = MagicMock()
    posts_resp.posts = [target_post]
    client.get_posts = AsyncMock(return_value=posts_resp)

    return client


# ---------------------------------------------------------------------------
# _post_text_and_facets
# ---------------------------------------------------------------------------

def test_text_url_only():
    text, facets = _post_text_and_facets(None, "https://example.com")
    assert text == "https://example.com"
    assert len(facets) == 1
    assert facets[0].features[0].uri == "https://example.com"


def test_text_with_title():
    text, facets = _post_text_and_facets("My Title", "https://example.com")
    assert text == "My Title\nhttps://example.com"
    assert len(facets) == 1


def test_text_facet_byte_offsets_are_correct():
    url = "https://example.com"
    text, facets = _post_text_and_facets("My Title", url)
    text_bytes = text.encode("utf-8")
    url_bytes = url.encode("utf-8")
    start = facets[0].index.byte_start
    end = facets[0].index.byte_end
    assert text_bytes[start:end] == url_bytes


def test_text_facet_byte_offsets_with_multibyte_title():
    # Non-ASCII title so byte offset != char offset.
    url = "https://example.com/path"
    title = "Ünïcödé"
    text, facets = _post_text_and_facets(title, url)
    text_bytes = text.encode("utf-8")
    url_bytes = url.encode("utf-8")
    extracted = text_bytes[facets[0].index.byte_start:facets[0].index.byte_end]
    assert extracted == url_bytes


def test_text_title_truncated_when_too_long():
    url = "https://example.com"
    long_title = "A" * 300
    text, _ = _post_text_and_facets(long_title, url)
    # grapheme count must not exceed the 300-grapheme limit
    assert len(text) <= 300
    # URL must still be present
    assert text.endswith(url)


# ---------------------------------------------------------------------------
# _build_labels
# ---------------------------------------------------------------------------

def test_labels_none_for_sfw_not_graphic():
    assert _build_labels(_submission(GraphicStatus.NOT_GRAPHIC.value), _board(nsfw=False)) is None


def test_labels_sexual_for_nsfw_board():
    result = _build_labels(_submission(), _board(nsfw=True))
    assert result is not None
    assert any(v.val == "sexual" for v in result.values)


def test_labels_graphic_media_for_graphic_submission():
    result = _build_labels(_submission(GraphicStatus.GRAPHIC.value), _board(nsfw=False))
    assert result is not None
    assert any(v.val == "graphic-media" for v in result.values)


def test_labels_both_when_nsfw_and_graphic():
    result = _build_labels(_submission(GraphicStatus.GRAPHIC.value), _board(nsfw=True))
    assert result is not None
    vals = {v.val for v in result.values}
    assert vals == {"sexual", "graphic-media"}


def test_labels_unknown_graphic_status_on_sfw_board_is_none():
    # GraphicStatus.UNKNOWN on a non-graphic board should not add graphic-media.
    result = _build_labels(_submission(GraphicStatus.UNKNOWN.value), _board(nsfw=False))
    assert result is None


# ---------------------------------------------------------------------------
# _determine_kind
# ---------------------------------------------------------------------------

def test_kind_record_for_bluesky_link():
    assert _determine_kind([_link(domain_family="bluesky")], False) == "record"


def test_kind_images_when_has_uploaded_image():
    assert _determine_kind([_link()], True) == "images"


def test_kind_external_for_link_without_uploaded_image():
    assert _determine_kind([_link()], False) == "external"


def test_kind_empty_when_no_links():
    assert _determine_kind([], False) == "empty"


def test_kind_bluesky_takes_precedence_over_uploaded_image():
    # Even with an uploaded image, a Bluesky primary link means record embed.
    assert _determine_kind([_link(domain_family="bluesky")], True) == "record"


# ---------------------------------------------------------------------------
# at_uri_to_url
# ---------------------------------------------------------------------------

def test_at_uri_to_url_standard():
    uri = "at://did:plc:abc123/app.bsky.feed.post/xyz789"
    assert at_uri_to_url(uri) == "https://bsky.app/profile/did:plc:abc123/post/xyz789"


def test_at_uri_to_url_with_explicit_handle():
    uri = "at://did:plc:abc123/app.bsky.feed.post/xyz789"
    assert at_uri_to_url(uri, "robots.exegesis.space") == "https://bsky.app/profile/robots.exegesis.space/post/xyz789"


def test_at_uri_to_url_did_used_when_no_handle():
    uri = "at://did:plc:abc123/app.bsky.feed.post/xyz789"
    assert at_uri_to_url(uri) == "https://bsky.app/profile/did:plc:abc123/post/xyz789"


def test_at_uri_to_url_malformed_returns_original():
    uri = "at://malformed"
    assert at_uri_to_url(uri) == uri


# ---------------------------------------------------------------------------
# _create_post
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_post_calls_create_record_with_correct_collection():
    client = _mock_client()
    result = await _create_post(
        client,
        text="https://example.com",
        facets=[],
        embed=None,
        labels=None,
    )
    assert result.success
    data = client.com.atproto.repo.create_record.call_args[0][0]
    assert data.collection == models.ids.AppBskyFeedPost
    assert data.repo == client.me.did


@pytest.mark.asyncio
async def test_create_post_record_contains_labels():
    client = _mock_client()
    labels = models.ComAtprotoLabelDefs.SelfLabels(
        values=[models.ComAtprotoLabelDefs.SelfLabel(val="sexual")]
    )
    await _create_post(client, text="test", facets=[], embed=None, labels=labels)
    record = client.com.atproto.repo.create_record.call_args[0][0].record
    assert record.labels is not None
    assert record.labels.values[0].val == "sexual"


@pytest.mark.asyncio
async def test_create_post_record_contains_no_labels_when_none():
    client = _mock_client()
    await _create_post(client, text="test", facets=[], embed=None, labels=None)
    record = client.com.atproto.repo.create_record.call_args[0][0].record
    assert record.labels is None


@pytest.mark.asyncio
async def test_create_post_returns_uri_and_cid():
    client = _mock_client(did="did:plc:specific")
    result = await _create_post(client, text="test", facets=[], embed=None, labels=None)
    assert result.at_uri == "at://did:plc:specific/app.bsky.feed.post/abc123"
    assert result.at_cid == "bafyreitest000"


# ---------------------------------------------------------------------------
# _publish_external
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_publish_external_uploads_thumbnail_when_path_present():
    client = _mock_client()
    link = _link(resolved_image_path="/data/thumb.jpg")

    with patch("bot.publish._upload_blob", new=AsyncMock(return_value=_fake_blob())) as mock_upload:
        result = await _publish_external(client, [link], labels=None, tags=[])

    assert result.success
    mock_upload.assert_called_once()


@pytest.mark.asyncio
async def test_publish_external_skips_upload_when_no_image_path():
    client = _mock_client()
    link = _link(resolved_image_path=None)

    with patch("bot.publish._upload_blob", new=AsyncMock()) as mock_upload:
        result = await _publish_external(client, [link], labels=None, tags=[])

    assert result.success
    mock_upload.assert_not_called()
    record = client.com.atproto.repo.create_record.call_args[0][0].record
    assert record.embed.external.thumb is None


@pytest.mark.asyncio
async def test_publish_external_embed_fields():
    client = _mock_client()
    link = _link(
        canonical_url="https://www.artstation.com/artwork/BmBmAA",
        resolved_title="Cool Art",
        resolved_description="A great piece",
        resolved_image_path=None,
    )
    await _publish_external(client, [link], labels=None, tags=[])
    ext = client.com.atproto.repo.create_record.call_args[0][0].record.embed.external
    assert ext.uri == "https://www.artstation.com/artwork/BmBmAA"
    assert ext.title == "Cool Art"
    assert ext.description == "A great piece"


@pytest.mark.asyncio
async def test_publish_external_text_contains_url():
    client = _mock_client()
    link = _link(canonical_url="https://www.artstation.com/artwork/BmBmAA", resolved_title="Art")
    await _publish_external(client, [link], labels=None, tags=[])
    record = client.com.atproto.repo.create_record.call_args[0][0].record
    assert "https://www.artstation.com/artwork/BmBmAA" in record.text


# ---------------------------------------------------------------------------
# _publish_images
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_publish_images_uploads_each_image():
    client = _mock_client()
    atts = [
        _attachment(local_path="/data/a.jpg", alt_text_body="first image"),
        _attachment(local_path="/data/b.jpg", alt_text_body="second image"),
    ]
    with patch("bot.publish._upload_blob", new=AsyncMock(return_value=_fake_blob())) as mock_upload:
        result = await _publish_images(client, [_link()], atts, labels=None, tags=[])

    assert result.success
    assert mock_upload.call_count == 2  # noqa: PLR2004


@pytest.mark.asyncio
async def test_publish_images_preserves_alt_text():
    client = _mock_client()
    atts = [_attachment(alt_text_body="a cute robot")]
    with patch("bot.publish._upload_blob", new=AsyncMock(return_value=_fake_blob())):
        await _publish_images(client, [_link()], atts, labels=None, tags=[])

    images = client.com.atproto.repo.create_record.call_args[0][0].record.embed.images
    assert images[0].alt == "a cute robot"


@pytest.mark.asyncio
async def test_publish_images_skips_non_image_attachments():
    client = _mock_client()
    atts = [
        _attachment(is_image=False, local_path="/data/doc.pdf"),
        _attachment(is_image=True, local_path="/data/img.jpg", alt_text_body="photo"),
    ]
    with patch("bot.publish._upload_blob", new=AsyncMock(return_value=_fake_blob())) as mock_upload:
        result = await _publish_images(client, [_link()], atts, labels=None, tags=[])

    assert result.success
    assert mock_upload.call_count == 1


@pytest.mark.asyncio
async def test_publish_images_fails_when_no_local_path():
    client = _mock_client()
    atts = [_attachment(local_path=None)]
    result = await _publish_images(client, [_link()], atts, labels=None, tags=[])
    assert not result.success
    assert result.error is not None and "no images" in result.error


@pytest.mark.asyncio
async def test_publish_images_text_contains_source_url():
    client = _mock_client()
    atts = [_attachment()]
    with patch("bot.publish._upload_blob", new=AsyncMock(return_value=_fake_blob())):
        await _publish_images(
            client,
            [_link(canonical_url="https://www.artstation.com/artwork/BmBmAA")],
            atts,
            labels=None,
            tags=[],
        )
    record = client.com.atproto.repo.create_record.call_args[0][0].record
    assert "https://www.artstation.com/artwork/BmBmAA" in record.text


# ---------------------------------------------------------------------------
# _append_tags
# ---------------------------------------------------------------------------

def test_append_tags_adds_text_and_facets():
    text, facets = _append_tags("hello", [], ["robots"])
    assert "#robots" in text
    assert len(facets) == 1
    assert facets[0].features[0].tag == "robots"


def test_append_tags_facet_byte_offsets():
    text, facets = _append_tags("hi", [], ["robots"])
    text_bytes = text.encode("utf-8")
    start = facets[0].index.byte_start
    end = facets[0].index.byte_end
    assert text_bytes[start:end] == b"#robots"


def test_append_tags_multiple_tags():
    text, facets = _append_tags("hi", [], ["robots", "art"])
    assert "#robots" in text
    assert "#art" in text
    assert len(facets) == 2


def test_append_tags_drops_tags_that_would_exceed_limit():
    # Start with text near the limit so the tag won't fit.
    text = "A" * 295
    text, facets = _append_tags(text, [], ["robots"])
    # " #robots" = 8 chars; 295 + 8 = 303 > 300, so tag should be dropped.
    assert "#robots" not in text
    assert len(facets) == 0


def test_append_tags_empty_list_is_noop():
    text, facets = _append_tags("hello", [], [])
    assert text == "hello"
    assert facets == []


# ---------------------------------------------------------------------------
# publish_submission - like and bsky_url
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_publish_submission_likes_own_post():
    board = _board()
    sub = _submission()
    link = _link(resolved_image_path=None)

    with (
        patch("bot.publish.AsyncClient") as MockClient,
        patch("bot.publish._upload_blob", new=AsyncMock(return_value=_fake_blob())),
    ):
        client = _mock_client()
        client.login = AsyncMock()
        client.like = AsyncMock()
        MockClient.return_value = client

        result = await publish_submission(sub, [link], [], board, "app-password")

    assert result.success
    client.like.assert_called_once_with(result.at_uri, result.at_cid)


@pytest.mark.asyncio
async def test_publish_submission_like_failure_does_not_fail_publish():
    board = _board()
    sub = _submission()
    link = _link(resolved_image_path=None)

    with (
        patch("bot.publish.AsyncClient") as MockClient,
        patch("bot.publish._upload_blob", new=AsyncMock(return_value=_fake_blob())),
    ):
        client = _mock_client()
        client.login = AsyncMock()
        client.like = AsyncMock(side_effect=Exception("rate limited"))
        MockClient.return_value = client

        result = await publish_submission(sub, [link], [], board, "app-password")

    assert result.success


@pytest.mark.asyncio
async def test_publish_submission_bsky_url_uses_handle():
    board = _board()
    sub = _submission()
    link = _link(resolved_image_path=None)

    with (
        patch("bot.publish.AsyncClient") as MockClient,
        patch("bot.publish._upload_blob", new=AsyncMock(return_value=_fake_blob())),
    ):
        client = _mock_client()
        client.login = AsyncMock()
        client.like = AsyncMock()
        MockClient.return_value = client

        result = await publish_submission(sub, [link], [], board, "app-password")

    assert result.bsky_url is not None
    assert "robots.exegesis.space" in result.bsky_url
    assert "did:plc" not in result.bsky_url


@pytest.mark.asyncio
async def test_publish_submission_nsfw_board_adds_nsfw_tag():
    board = _board(nsfw=True, tags=[])
    sub = _submission()
    link = _link(resolved_image_path=None)

    with (
        patch("bot.publish.AsyncClient") as MockClient,
        patch("bot.publish._upload_blob", new=AsyncMock(return_value=_fake_blob())),
    ):
        client = _mock_client()
        client.login = AsyncMock()
        client.like = AsyncMock()
        MockClient.return_value = client

        await publish_submission(sub, [link], [], board, "app-password")

    record = client.com.atproto.repo.create_record.call_args[0][0].record
    assert "#nsfw" in record.text


@pytest.mark.asyncio
async def test_publish_submission_board_tags_appended():
    board = _board(tags=["robot-posting"])
    sub = _submission()
    link = _link(resolved_image_path=None)

    with (
        patch("bot.publish.AsyncClient") as MockClient,
        patch("bot.publish._upload_blob", new=AsyncMock(return_value=_fake_blob())),
    ):
        client = _mock_client()
        client.login = AsyncMock()
        client.like = AsyncMock()
        MockClient.return_value = client

        await publish_submission(sub, [link], [], board, "app-password")

    record = client.com.atproto.repo.create_record.call_args[0][0].record
    assert "#robot-posting" in record.text


# ---------------------------------------------------------------------------
# _resolve_bluesky_post
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_bluesky_post_with_did_skips_resolve_handle():
    client = _mock_client()
    at_uri, cid = await _resolve_bluesky_post(
        client, "https://bsky.app/profile/did:plc:targetdid000/post/xyz789"
    )
    client.resolve_handle.assert_not_called()
    assert at_uri == "at://did:plc:targetdid000/app.bsky.feed.post/xyz789"
    assert cid == "bafyreitarget000"


@pytest.mark.asyncio
async def test_resolve_bluesky_post_with_handle_calls_resolve_handle():
    client = _mock_client()
    at_uri, cid = await _resolve_bluesky_post(
        client, "https://bsky.app/profile/someone.bsky.social/post/xyz789"
    )
    client.resolve_handle.assert_called_once_with("someone.bsky.social")
    assert at_uri == "at://did:plc:targetdid000/app.bsky.feed.post/xyz789"
    assert cid == "bafyreitarget000"


@pytest.mark.asyncio
async def test_resolve_bluesky_post_raises_when_not_found():
    client = _mock_client()
    client.get_posts = AsyncMock(return_value=MagicMock(posts=[]))
    with pytest.raises(ValueError, match="not found"):
        await _resolve_bluesky_post(
            client, "https://bsky.app/profile/did:plc:gone/post/missing"
        )


# ---------------------------------------------------------------------------
# _publish_record
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_publish_record_calls_repost_with_resolved_uri_and_cid():
    client = _mock_client()
    link = _link(
        canonical_url="https://bsky.app/profile/did:plc:targetdid000/post/xyz789",
        domain_family="bluesky",
    )
    result = await _publish_record(client, [link])
    assert result.success
    assert result.is_repost
    client.repost.assert_called_once_with(
        "at://did:plc:targetdid000/app.bsky.feed.post/xyz789",
        "bafyreitarget000",
    )


@pytest.mark.asyncio
async def test_publish_record_returns_failure_when_resolve_fails():
    client = _mock_client()
    client.get_posts = AsyncMock(return_value=MagicMock(posts=[]))
    link = _link(
        canonical_url="https://bsky.app/profile/did:plc:gone/post/missing",
        domain_family="bluesky",
    )
    result = await _publish_record(client, [link])
    assert not result.success
    assert result.error is not None and "resolve" in result.error


# ---------------------------------------------------------------------------
# _publish_reply_post
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_publish_reply_post_includes_reply_ref():
    client = _mock_client()
    link = _link(canonical_url="https://www.artstation.com/artwork/extra")
    root_uri = "at://did:plc:testdid000/app.bsky.feed.post/root1"
    root_cid = "bafyreiroot000"

    reply_uri, reply_cid = await _publish_reply_post(
        client, link, labels=None, tags=[],
        root_uri=root_uri, root_cid=root_cid,
        parent_uri=root_uri, parent_cid=root_cid,
    )

    record = client.com.atproto.repo.create_record.call_args[0][0].record
    assert record.reply is not None
    assert record.reply.root.uri == root_uri
    assert record.reply.root.cid == root_cid
    assert record.reply.parent.uri == root_uri
    assert reply_uri == "at://did:plc:testdid000/app.bsky.feed.post/abc123"


@pytest.mark.asyncio
async def test_publish_reply_post_text_contains_link_url():
    client = _mock_client()
    link = _link(canonical_url="https://www.artstation.com/artwork/extra", resolved_title="Extra Art")
    await _publish_reply_post(
        client, link, labels=None, tags=[],
        root_uri="at://x/app.bsky.feed.post/r", root_cid="cid1",
        parent_uri="at://x/app.bsky.feed.post/r", parent_cid="cid1",
    )
    record = client.com.atproto.repo.create_record.call_args[0][0].record
    assert "https://www.artstation.com/artwork/extra" in record.text


# ---------------------------------------------------------------------------
# publish_submission - native repost (record kind)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_publish_submission_record_kind_calls_repost():
    board = _board(tags=[])
    sub = _submission()
    link = _link(
        canonical_url="https://bsky.app/profile/did:plc:targetdid000/post/xyz789",
        domain_family="bluesky",
    )

    with patch("bot.publish.AsyncClient") as MockClient:
        client = _mock_client()
        client.login = AsyncMock()
        MockClient.return_value = client

        result = await publish_submission(sub, [link], [], board, "app-password")

    assert result.success
    assert result.is_repost
    client.repost.assert_called_once()
    # Like must NOT be called for native reposts.
    client.like.assert_not_called()


@pytest.mark.asyncio
async def test_publish_submission_record_kind_bsky_url_is_original_post():
    board = _board(tags=[])
    sub = _submission()
    original_url = "https://bsky.app/profile/did:plc:targetdid000/post/xyz789"
    link = _link(canonical_url=original_url, domain_family="bluesky")

    with patch("bot.publish.AsyncClient") as MockClient:
        client = _mock_client()
        client.login = AsyncMock()
        MockClient.return_value = client

        result = await publish_submission(sub, [link], [], board, "app-password")

    assert result.bsky_url == original_url


# ---------------------------------------------------------------------------
# publish_submission - multi-link reply thread
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_publish_submission_multi_link_creates_reply_posts():
    board = _board(tags=[])
    sub = _submission()
    links = [
        _link(canonical_url="https://www.artstation.com/artwork/BmBmAA", resolved_image_path=None),
        _link(canonical_url="https://www.artstation.com/artwork/extra", resolved_image_path=None),
    ]

    with (
        patch("bot.publish.AsyncClient") as MockClient,
        patch("bot.publish._upload_blob", new=AsyncMock(return_value=_fake_blob())),
    ):
        client = _mock_client()
        client.login = AsyncMock()
        client.like = AsyncMock()
        MockClient.return_value = client

        result = await publish_submission(sub, links, [], board, "app-password")

    assert result.success
    # create_record called twice: primary post + one reply.
    assert client.com.atproto.repo.create_record.call_count == 2  # noqa: PLR2004
    reply_record = client.com.atproto.repo.create_record.call_args_list[1][0][0].record
    assert reply_record.reply is not None
    assert "artstation.com/artwork/extra" in reply_record.text


@pytest.mark.asyncio
async def test_publish_submission_single_link_no_reply_posts():
    board = _board(tags=[])
    sub = _submission()
    link = _link(resolved_image_path=None)

    with (
        patch("bot.publish.AsyncClient") as MockClient,
        patch("bot.publish._upload_blob", new=AsyncMock(return_value=_fake_blob())),
    ):
        client = _mock_client()
        client.login = AsyncMock()
        client.like = AsyncMock()
        MockClient.return_value = client

        await publish_submission(sub, [link], [], board, "app-password")

    assert client.com.atproto.repo.create_record.call_count == 1


# ---------------------------------------------------------------------------
# at_uri_to_url - repost record type returns AT URI unchanged
# ---------------------------------------------------------------------------

def test_at_uri_to_url_repost_record_returns_at_uri():
    repost_uri = "at://did:plc:abc/app.bsky.feed.repost/rp1"
    assert at_uri_to_url(repost_uri) == repost_uri
