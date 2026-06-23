from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.resolve.fetch import (
    _deviantart_mirror_url,
    _instagram_mirror_url,
    _reddit_mirror_url,
    _twitter_mirror_url,
    parse_html_metadata,
    resolve,
)


# --- parse_html_metadata ---

def test_opengraph_preferred():
    html = """
    <html><head>
      <title>Fallback Title</title>
      <meta property="og:title" content="OG Title">
      <meta property="og:description" content="A description">
      <meta property="og:image" content="https://cdn.example.com/img.jpg">
    </head><body>x</body></html>
    """
    m = parse_html_metadata(html, "https://example.com/page")
    assert m.title == "OG Title"
    assert m.description == "A description"
    assert m.image_url == "https://cdn.example.com/img.jpg"
    assert m.via == "opengraph"


def test_falls_back_to_title_tag():
    html = "<html><head><title>  Just a Title  </title></head><body></body></html>"
    m = parse_html_metadata(html, "https://example.com/")
    assert m.title == "Just a Title"
    assert m.image_url is None
    assert m.via == "html"


def test_twitter_card_and_relative_image_resolved():
    html = """
    <head>
      <meta name="twitter:title" content="Tw Title">
      <meta name="twitter:image" content="/rel/pic.png">
    </head>
    """
    m = parse_html_metadata(html, "https://example.com/a/b")
    assert m.title == "Tw Title"
    # relative image resolved against the (final) base URL
    assert m.image_url == "https://example.com/rel/pic.png"


def test_no_metadata():
    m = parse_html_metadata("<html><body>nothing</body></html>", "https://example.com/")
    assert m.title is None
    assert m.via == "none"


# --- mirror URL helpers ---

def test_twitter_mirror_url():
    assert _twitter_mirror_url("https://twitter.com/user/status/123") == (
        "https://fxtwitter.com/user/status/123"
    )


def test_reddit_mirror_url():
    assert _reddit_mirror_url("https://www.reddit.com/r/art/comments/abc/") == (
        "https://vxreddit.com/r/art/comments/abc/"
    )


def test_instagram_mirror_url():
    assert _instagram_mirror_url("https://www.instagram.com/p/ABC/") == (
        "https://kkinstagram.com/p/ABC/"
    )


def test_deviantart_mirror_url():
    assert _deviantart_mirror_url("https://www.deviantart.com/alice/art/Title-123") == (
        "https://fixdeviantart.com/alice/art/Title-123"
    )


# --- resolve() integration (all network calls mocked) ---

def _html_response(title="Post Title", image="https://cdn.example.com/img.jpg") -> MagicMock:
    body = f"""<html><head>
      <meta property="og:title" content="{title}">
      <meta property="og:image" content="{image}">
    </head></html>"""
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {"content-type": "text/html"}
    resp.text = body
    resp.url = "https://example.com/"
    resp.raise_for_status = MagicMock()
    return resp


def _oembed_response(title="OEmbed Title", thumb="https://cdn.example.com/thumb.jpg") -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json = MagicMock(return_value={"title": title, "author_name": "Artist", "thumbnail_url": thumb})
    return resp


def _error_response(status=404) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    return resp


@pytest.mark.asyncio
async def test_resolve_bluesky_skipped():
    client = AsyncMock()
    result = await resolve("https://bsky.app/profile/a/post/b", "bluesky", client=client)
    assert result.via == "skipped"
    client.get.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_youtube_uses_oembed():
    client = AsyncMock()
    client.get.return_value = _oembed_response("Rick Roll")
    result = await resolve(
        "https://youtu.be/dQw4w9WgXcQ", "youtube", client=client
    )
    assert result.title == "Rick Roll"
    assert result.via == "oembed"
    # oEmbed endpoint should be called
    call_url = client.get.call_args[0][0]
    assert "youtube.com/oembed" in call_url


@pytest.mark.asyncio
async def test_resolve_deviantart_uses_oembed():
    client = AsyncMock()
    client.get.return_value = _oembed_response("Cool Drawing", "https://cdn.da.com/thumb.jpg")
    result = await resolve(
        "https://www.deviantart.com/alice/art/Cool-Drawing-123",
        "deviantart",
        client=client,
    )
    assert result.title == "Cool Drawing"
    assert result.via == "oembed"
    call_url = client.get.call_args[0][0]
    assert "backend.deviantart.com/oembed" in call_url


@pytest.mark.asyncio
async def test_resolve_deviantart_oembed_404_falls_to_mirror():
    client = AsyncMock()
    # First call (oEmbed) → 404; second call (fixdeviantart mirror) → HTML with og tags
    client.get.side_effect = [_error_response(404), _html_response("Mirror Title")]
    result = await resolve(
        "https://www.deviantart.com/alice/art/Cool-Drawing-123",
        "deviantart",
        client=client,
    )
    assert result.title == "Mirror Title"
    assert client.get.call_count == 2  # noqa: PLR2004
    mirror_call_url = client.get.call_args_list[1][0][0]
    assert "fixdeviantart.com" in mirror_call_url


@pytest.mark.asyncio
async def test_resolve_twitter_uses_fxtwitter_mirror():
    client = AsyncMock()
    # Skip oEmbed (no handler), go straight to mirror fetch.
    client.get.return_value = _html_response("Tweet text here")
    result = await resolve("https://twitter.com/user/status/123", "twitter", client=client)
    assert result.title == "Tweet text here"
    call_url = client.get.call_args[0][0]
    assert "fxtwitter.com" in call_url


@pytest.mark.asyncio
async def test_resolve_reddit_uses_vxreddit_mirror():
    client = AsyncMock()
    client.get.return_value = _html_response("A Reddit post")
    result = await resolve(
        "https://www.reddit.com/r/art/comments/abc/title/", "reddit", client=client
    )
    assert result.title == "A Reddit post"
    call_url = client.get.call_args[0][0]
    assert "vxreddit.com" in call_url


@pytest.mark.asyncio
async def test_resolve_instagram_uses_kkinstagram_mirror():
    client = AsyncMock()
    client.get.return_value = _html_response("An insta post")
    result = await resolve("https://www.instagram.com/p/ABC123/", "instagram", client=client)
    assert result.title == "An insta post"
    call_url = client.get.call_args[0][0]
    assert "kkinstagram.com" in call_url


@pytest.mark.asyncio
async def test_resolve_falls_to_discord_fallback_when_all_fail():
    import httpx

    client = AsyncMock()
    client.get.side_effect = httpx.NetworkError("timeout")
    result = await resolve(
        "https://twitter.com/user/status/123",
        "twitter",
        client=client,
        fallback_title="Discord title",
        fallback_image_url="https://cdn.discord.com/img.jpg",
    )
    assert result.title == "Discord title"
    assert result.via == "discord"


@pytest.mark.asyncio
async def test_resolve_other_family_fetches_directly():
    client = AsyncMock()
    client.get.return_value = _html_response("Some site")
    result = await resolve("https://example.com/page", "other", client=client)
    assert result.title == "Some site"
    call_url = client.get.call_args[0][0]
    assert "example.com" in call_url
