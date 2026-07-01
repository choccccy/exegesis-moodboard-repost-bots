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
async def test_resolve_twitter_fxtwitter_api_returns_image():
    client = AsyncMock()
    api_resp = MagicMock()
    api_resp.status_code = 200
    api_resp.json.return_value = {
        "tweet": {
            "text": "cool robot pic",
            "author": {"name": "RobotPoster"},
            "media": {"photos": [{"url": "https://pbs.twimg.com/media/abc.jpg"}]},
        }
    }
    client.get.return_value = api_resp
    result = await resolve("https://twitter.com/user/status/123", "twitter", client=client)
    assert result.image_url == "https://pbs.twimg.com/media/abc.jpg"
    assert result.via == "fxtwitter_api"
    assert "api.fxtwitter.com" in client.get.call_args_list[0][0][0]


@pytest.mark.asyncio
async def test_resolve_twitter_fxtwitter_api_qrt_uses_quoted_image():
    client = AsyncMock()
    api_resp = MagicMock()
    api_resp.status_code = 200
    api_resp.json.return_value = {
        "tweet": {
            "text": "me and who",
            "author": {"name": "skyacinth_"},
            "media": {},
            "quote": {
                "text": "removing ram from a computer while it's on",
                "author": {"name": "pc_98s"},
                "media": {"photos": [{"url": "https://pbs.twimg.com/media/qrt.jpg"}]},
            },
        }
    }
    client.get.return_value = api_resp
    result = await resolve("https://twitter.com/skyacinth_/status/1802338297739436052", "twitter", client=client)
    assert result.image_url == "https://pbs.twimg.com/media/qrt.jpg"
    assert result.via == "fxtwitter_api"


@pytest.mark.asyncio
async def test_resolve_twitter_falls_back_to_mirror_when_api_fails():
    client = AsyncMock()
    # fxtwitter API → 404; vxtwitter API → 404; fxtwitter OG mirror → HTML
    client.get.side_effect = [
        _error_response(404),
        _error_response(404),
        _html_response("Tweet text here"),
    ]
    result = await resolve("https://twitter.com/user/status/123", "twitter", client=client)
    assert result.title == "Tweet text here"
    mirror_url = client.get.call_args_list[2][0][0]
    assert "fxtwitter.com" in mirror_url


@pytest.mark.asyncio
async def test_resolve_twitter_vxtwitter_empty_body_falls_to_mirror():
    """vxtwitter returns 200 with an empty/non-JSON body - should fall through to mirror."""
    empty = MagicMock()
    empty.status_code = 200
    empty.json.side_effect = ValueError("Expecting value: line 1 column 1 (char 0)")
    client = AsyncMock()
    client.get.side_effect = [
        _error_response(404),       # fxtwitter API
        empty,                      # vxtwitter API - 200 but bad JSON
        _html_response("Tweet text here"),  # fxtwitter OG mirror
    ]
    result = await resolve("https://twitter.com/user/status/123", "twitter", client=client)
    assert result.title == "Tweet text here"
    assert client.get.call_count == 3  # noqa: PLR2004


@pytest.mark.asyncio
async def test_resolve_instagram_uses_oembed():
    client = AsyncMock()
    client.get.return_value = _oembed_response("More hands patting Cria", "https://cdn.instagram.com/thumb.jpg")
    result = await resolve("https://www.instagram.com/reel/DM2dwYTy_6s/", "instagram", client=client)
    assert result.title == "More hands patting Cria"
    assert result.via == "oembed"
    call_url = client.get.call_args[0][0]
    assert "instagram.com/api/v1/oembed" in call_url



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
async def test_resolve_instagram_oembed_404_falls_to_mirror_existing():
    # Renamed from test_resolve_instagram_uses_kkinstagram_mirror - oEmbed is tried first now.
    client = AsyncMock()
    client.get.side_effect = [_error_response(404), _html_response("An insta post")]
    result = await resolve("https://www.instagram.com/p/ABC123/", "instagram", client=client)
    assert result.title == "An insta post"
    call_url = client.get.call_args_list[1][0][0]
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


@pytest.mark.asyncio
async def test_resolve_tumblr_uses_oembed():
    client = AsyncMock()
    client.get.return_value = _oembed_response("Cool Robot Post")
    result = await resolve(
        "https://username.tumblr.com/post/123456789", "tumblr", client=client
    )
    assert result.title == "Cool Robot Post"
    assert result.via == "oembed"
    call_url = client.get.call_args[0][0]
    assert "tumblr.com/oembed" in call_url


@pytest.mark.asyncio
async def test_resolve_tumblr_photo_post_uses_url_as_image():
    client = AsyncMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.json = MagicMock(return_value={
        "type": "photo",
        "url": "https://cdn.tumblr.com/full.jpg",
        "thumbnail_url": "https://cdn.tumblr.com/thumb.jpg",
        "author_name": "cool-blog",
    })
    client.get.return_value = resp
    result = await resolve(
        "https://cool-blog.tumblr.com/post/123456789", "tumblr", client=client
    )
    assert result.image_url == "https://cdn.tumblr.com/full.jpg"
    assert result.via == "oembed"


@pytest.mark.asyncio
async def test_resolve_tumblr_oembed_404_falls_to_opengraph():
    client = AsyncMock()
    client.get.side_effect = [_error_response(404), _html_response("Tumblr OG Title")]
    result = await resolve(
        "https://username.tumblr.com/post/123456789", "tumblr", client=client
    )
    assert result.title == "Tumblr OG Title"
    assert client.get.call_count == 2  # noqa: PLR2004


@pytest.mark.asyncio
async def test_resolve_tiktok_uses_tikwm_for_thumbnail():
    client = AsyncMock()
    tikwm_resp = MagicMock()
    tikwm_resp.status_code = 200
    tikwm_resp.json = MagicMock(return_value={
        "code": 0,
        "data": {
            "cover": "https://cdn.tiktok.com/cover.jpg",
            "author": {"nickname": "Izidor Sustersic"},
        },
    })
    client.get.return_value = tikwm_resp
    result = await resolve(
        "https://www.tiktok.com/@izidorsustersic/video/7541890284209179927",
        "tiktok",
        client=client,
        fallback_title="Mountain bike reel caption",
    )
    assert result.title == "Mountain bike reel caption"
    assert result.image_url == "https://cdn.tiktok.com/cover.jpg"
    assert result.description == "Izidor Sustersic"
    assert result.via == "tikwm"
    call_url = client.get.call_args[0][0]
    assert "tikwm.com/api" in call_url


@pytest.mark.asyncio
async def test_resolve_tiktok_tikwm_failure_falls_to_direct_fetch():
    client = AsyncMock()
    client.get.side_effect = [_error_response(500), _html_response("TikTok - Make Your Day")]
    result = await resolve(
        "https://www.tiktok.com/@someone/video/123", "tiktok", client=client
    )
    assert client.get.call_count == 2  # noqa: PLR2004
