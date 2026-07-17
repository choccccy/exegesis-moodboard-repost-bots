from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import httpx

from bot.resolve.fetch import (
    _deviantart_mirror_url,
    _instagram_mirror_url,
    _reddit_mirror_url,
    _twitter_mirror_url,
    parse_html_metadata,
    resolve,
    resolve_bluesky_at_uri,
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


def _resolve_handle_response(did: str):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"did": did}
    return resp


@pytest.mark.asyncio
async def test_resolve_bluesky_at_uri_resolves_handle_to_did():
    client = AsyncMock()
    client.get.return_value = _resolve_handle_response("did:plc:abc123")
    at_uri = await resolve_bluesky_at_uri(
        "https://bsky.app/profile/someone.bsky.social/post/rk9", client
    )
    assert at_uri == "at://did:plc:abc123/app.bsky.feed.post/rk9"
    call = client.get.call_args
    assert "resolveHandle" in call[0][0]
    assert call.kwargs["params"] == {"handle": "someone.bsky.social"}


@pytest.mark.asyncio
async def test_resolve_bluesky_at_uri_did_url_skips_network():
    client = AsyncMock()
    at_uri = await resolve_bluesky_at_uri(
        "https://bsky.app/profile/did:plc:xyz789/post/rk1", client
    )
    assert at_uri == "at://did:plc:xyz789/app.bsky.feed.post/rk1"
    client.get.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_bluesky_at_uri_returns_none_on_resolve_failure():
    client = AsyncMock()
    client.get.side_effect = httpx.HTTPError("boom")
    at_uri = await resolve_bluesky_at_uri(
        "https://bsky.app/profile/gone.bsky.social/post/rk1", client
    )
    assert at_uri is None


@pytest.mark.asyncio
async def test_resolve_bluesky_at_uri_returns_none_on_malformed_url():
    client = AsyncMock()
    at_uri = await resolve_bluesky_at_uri("https://bsky.app/notapost", client)
    assert at_uri is None
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
async def test_resolve_twitter_fxtwitter_api_gif_uses_thumbnail():
    client = AsyncMock()
    api_resp = MagicMock()
    api_resp.status_code = 200
    api_resp.json.return_value = {
        "tweet": {
            "text": "lol look at this",
            "author": {"name": "SussySpongey"},
            "media": {
                "videos": [{
                    "url": "https://video.twimg.com/tweet_video/G7lhWQHXAAEfxeK.mp4",
                    "thumbnail_url": "https://pbs.twimg.com/tweet_video_thumb/G7lhWQHXAAEfxeK.jpg",
                    "type": "gif",
                }]
            },
        }
    }
    client.get.return_value = api_resp
    result = await resolve("https://twitter.com/i/status/1997734955288612915", "twitter", client=client)
    assert result.image_url == "https://pbs.twimg.com/tweet_video_thumb/G7lhWQHXAAEfxeK.jpg"
    assert result.via == "fxtwitter_api"


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
async def test_resolve_twitter_age_restricted_reports_unavailable():
    """fxtwitter 401 PRIVATE_TWEET + vxtwitter scan-failure (200 non-JSON) => 'unavailable',
    and the mirror/canonical OG steps are short-circuited (no junk 'post doesn't exist' card)."""
    scan_fail = MagicMock()
    scan_fail.status_code = 200
    scan_fail.json.side_effect = ValueError("not json")
    client = AsyncMock()
    client.get.side_effect = [
        _error_response(401),  # fxtwitter API - PRIVATE_TWEET
        scan_fail,             # vxtwitter API - 200 HTML error page
    ]
    result = await resolve("https://twitter.com/user/status/123", "twitter", client=client)
    assert result.via == "unavailable"
    assert result.title is None and result.image_url is None
    assert client.get.call_count == 2  # noqa: PLR2004 - no fall-through to OG mirror


@pytest.mark.asyncio
async def test_resolve_twitter_403_also_unavailable():
    """A 403 from fxtwitter (blocked) with no vxtwitter recovery is likewise 'unavailable'."""
    client = AsyncMock()
    client.get.side_effect = [_error_response(403), _error_response(500)]
    result = await resolve("https://twitter.com/user/status/123", "twitter", client=client)
    assert result.via == "unavailable"


@pytest.mark.asyncio
async def test_resolve_twitter_unavailable_honors_discord_fallback():
    """When Discord already captured an embed, an age-restricted tweet uses that (via=discord)
    rather than surfacing 'unavailable'."""
    scan_fail = MagicMock()
    scan_fail.status_code = 200
    scan_fail.json.side_effect = ValueError("not json")
    client = AsyncMock()
    client.get.side_effect = [_error_response(401), scan_fail]
    result = await resolve(
        "https://twitter.com/user/status/123", "twitter", client=client,
        fallback_title="Discord unfurled this", fallback_image_url="https://cdn/x.jpg",
    )
    assert result.via == "discord"
    assert result.title == "Discord unfurled this"


@pytest.mark.asyncio
async def test_resolve_twitter_401_but_vxtwitter_recovers():
    """fxtwitter 401 but vxtwitter succeeds => use vxtwitter, not 'unavailable'."""
    vx = MagicMock()
    vx.status_code = 200
    vx.json.return_value = {
        "text": "recovered tweet", "user_name": "Poster",
        "media_extended": [{"type": "image", "url": "https://pbs.twimg.com/media/ok.jpg"}],
    }
    client = AsyncMock()
    client.get.side_effect = [_error_response(401), vx]
    result = await resolve("https://twitter.com/user/status/123", "twitter", client=client)
    assert result.via == "vxtwitter_api"
    assert result.image_url == "https://pbs.twimg.com/media/ok.jpg"


@pytest.mark.asyncio
async def test_resolve_instagram_uses_oembed():
    client = AsyncMock()
    client.get.return_value = _oembed_response("More hands patting Cria", "https://cdn.instagram.com/thumb.jpg")
    result = await resolve("https://www.instagram.com/reel/DM2dwYTy_6s/", "instagram", client=client)
    assert result.title == "More hands patting Cria"
    assert result.via == "oembed"
    call_url = client.get.call_args[0][0]
    assert "instagram.com/api/v1/oembed" in call_url



def _reddit_api_response(post_data: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json = MagicMock(return_value=[{"data": {"children": [{"data": post_data}]}}])
    return resp


@pytest.mark.asyncio
async def test_resolve_reddit_json_api_direct_image():
    client = AsyncMock()
    client.get.return_value = _reddit_api_response({
        "title": "A cool car",
        "subreddit_name_prefixed": "r/WeirdWheels",
        "url": "https://i.redd.it/abc123.jpg",
        "crosspost_parent_list": [],
    })
    result = await resolve("https://www.reddit.com/r/WeirdWheels/comments/abc/cool_car/", "reddit", client=client)
    assert result.title == "A cool car"
    assert result.image_url == "https://i.redd.it/abc123.jpg"
    assert result.via == "reddit_api"
    assert "reddit.com" in client.get.call_args[0][0]
    assert client.get.call_args[0][0].endswith(".json?limit=1")


@pytest.mark.asyncio
async def test_resolve_reddit_json_api_crosspost_uses_parent_image():
    """Crossposts have no direct image; the original post's image is in crosspost_parent_list."""
    client = AsyncMock()
    client.get.return_value = _reddit_api_response({
        "title": "German Land Rover enthusiasts",
        "subreddit_name_prefixed": "r/WeirdWheels",
        "url": "https://www.reddit.com/r/LandRover/comments/original/",
        "crosspost_parent_list": [{
            "url": "https://i.redd.it/the_real_image.jpg",
            "preview": {},
        }],
    })
    result = await resolve(
        "https://www.reddit.com/r/WeirdWheels/comments/1ps0nb9/german_land_rover_enthusiasts/",
        "reddit",
        client=client,
    )
    assert result.title == "German Land Rover enthusiasts"
    assert result.image_url == "https://i.redd.it/the_real_image.jpg"
    assert result.via == "reddit_api"


@pytest.mark.asyncio
async def test_resolve_reddit_api_failure_falls_to_vxreddit_mirror():
    client = AsyncMock()
    # JSON API fails; vxreddit mirror succeeds.
    client.get.side_effect = [_error_response(429), _html_response("A Reddit post")]
    result = await resolve(
        "https://www.reddit.com/r/art/comments/abc/title/", "reddit", client=client
    )
    assert result.title == "A Reddit post"
    assert client.get.call_count == 2  # noqa: PLR2004
    mirror_url = client.get.call_args_list[1][0][0]
    assert "vxreddit.com" in mirror_url


def _wikipedia_api_response(title, description, image_url=None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    data = {"title": title, "description": description}
    if image_url:
        data["originalimage"] = {"source": image_url, "width": 1280, "height": 720}
    resp.json = MagicMock(return_value=data)
    return resp


@pytest.mark.asyncio
async def test_resolve_wikipedia_uses_rest_api():
    client = AsyncMock()
    client.get.return_value = _wikipedia_api_response(
        "Oshkosh NGDV",
        "2020s replacement for the US Postal Service's local delivery fleet",
        "https://upload.wikimedia.org/wikipedia/commons/thumb/d/de/USPS.jpg/1280px-USPS.jpg",
    )
    result = await resolve(
        "https://en.wikipedia.org/wiki/Oshkosh_NGDV", "wikipedia", client=client
    )
    assert result.title == "Oshkosh NGDV"
    assert result.image_url == "https://upload.wikimedia.org/wikipedia/commons/thumb/d/de/USPS.jpg/1280px-USPS.jpg"
    assert result.via == "wikipedia_api"
    api_url = client.get.call_args[0][0]
    assert "en.wikipedia.org/api/rest_v1/page/summary/Oshkosh_NGDV" in api_url


@pytest.mark.asyncio
async def test_resolve_wikipedia_no_image_returns_title_only():
    client = AsyncMock()
    client.get.return_value = _wikipedia_api_response(
        "Halting problem",
        "Undecidable problem in computability theory",
    )
    result = await resolve(
        "https://en.wikipedia.org/wiki/Halting_problem", "wikipedia", client=client
    )
    assert result.title == "Halting problem"
    assert result.image_url is None
    assert result.via == "wikipedia_api"


@pytest.mark.asyncio
async def test_resolve_wikipedia_api_failure_falls_to_opengraph():
    client = AsyncMock()
    client.get.side_effect = [_error_response(503), _html_response("Halting problem")]
    result = await resolve(
        "https://en.wikipedia.org/wiki/Halting_problem", "wikipedia", client=client
    )
    assert result.title == "Halting problem"
    assert client.get.call_count == 2  # noqa: PLR2004
    # Second call should be the direct OpenGraph fetch
    assert "en.wikipedia.org/wiki/Halting_problem" in client.get.call_args_list[1][0][0]


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


# --- video URL extraction (twitter GIFs/videos, tiktok, reddit GIFs) ---
#
# Regression/feature: video and GIF posts previously resolved to only a
# thumbnail still, so the bot published a static image instead of the actual
# video. The resolvers now surface a direct mp4 URL for ingestion to download.


@pytest.mark.asyncio
async def test_resolve_twitter_video_returns_video_url():
    client = AsyncMock()
    api_resp = MagicMock()
    api_resp.status_code = 200
    api_resp.json.return_value = {
        "tweet": {
            "text": "launch day",
            "author": {"name": "SpaceX"},
            "media": {
                "videos": [{
                    "url": "https://video.twimg.com/ext_tw_video/1/pu/vid/avc1/1920x1080/clip.mp4",
                    "thumbnail_url": "https://pbs.twimg.com/ext_tw_video_thumb/1/pu/img/still.jpg",
                    "width": 1920,
                    "height": 1080,
                    "type": "video",
                }]
            },
        }
    }
    client.get.return_value = api_resp
    result = await resolve("https://twitter.com/SpaceX/status/1", "twitter", client=client)
    assert result.video_url == "https://video.twimg.com/ext_tw_video/1/pu/vid/avc1/1920x1080/clip.mp4"
    assert result.video_width == 1920  # noqa: PLR2004
    assert result.video_height == 1080  # noqa: PLR2004
    assert result.image_url == "https://pbs.twimg.com/ext_tw_video_thumb/1/pu/img/still.jpg"


@pytest.mark.asyncio
async def test_resolve_twitter_gif_returns_video_url():
    client = AsyncMock()
    api_resp = MagicMock()
    api_resp.status_code = 200
    api_resp.json.return_value = {
        "tweet": {
            "text": "lol",
            "author": {"name": "Poster"},
            "media": {
                "videos": [{
                    "url": "https://video.twimg.com/tweet_video/G7lhWQHXAAEfxeK.mp4",
                    "thumbnail_url": "https://pbs.twimg.com/tweet_video_thumb/G7lhWQHXAAEfxeK.jpg",
                    "width": 480,
                    "height": 480,
                    "type": "gif",
                }]
            },
        }
    }
    client.get.return_value = api_resp
    result = await resolve("https://twitter.com/user/status/2", "twitter", client=client)
    assert result.video_url == "https://video.twimg.com/tweet_video/G7lhWQHXAAEfxeK.mp4"
    assert result.image_url == "https://pbs.twimg.com/tweet_video_thumb/G7lhWQHXAAEfxeK.jpg"


@pytest.mark.asyncio
async def test_resolve_twitter_photo_has_no_video_url():
    client = AsyncMock()
    api_resp = MagicMock()
    api_resp.status_code = 200
    api_resp.json.return_value = {
        "tweet": {
            "text": "pic",
            "author": {"name": "Poster"},
            "media": {"photos": [{"url": "https://pbs.twimg.com/media/abc.jpg"}]},
        }
    }
    client.get.return_value = api_resp
    result = await resolve("https://twitter.com/user/status/3", "twitter", client=client)
    assert result.video_url is None
    assert result.image_url == "https://pbs.twimg.com/media/abc.jpg"


@pytest.mark.asyncio
async def test_resolve_vxtwitter_video_uses_thumbnail_not_mp4_as_image():
    # Regression: the vxtwitter fallback used mediaURLs[0] as image_url, which
    # for video tweets is the .mp4 itself - the thumbnail downloader then saved
    # a video file as the "image". media_extended distinguishes the two.
    client = AsyncMock()
    vx_resp = MagicMock()
    vx_resp.status_code = 200
    vx_resp.json.return_value = {
        "text": "vroom",
        "user_name": "Cars",
        "mediaURLs": ["https://video.twimg.com/ext_tw_video/9/pu/vid/avc1/1280x720/v.mp4"],
        "media_extended": [{
            "type": "video",
            "url": "https://video.twimg.com/ext_tw_video/9/pu/vid/avc1/1280x720/v.mp4",
            "thumbnail_url": "https://pbs.twimg.com/ext_tw_video_thumb/9/pu/img/still.jpg",
            "size": {"width": 1280, "height": 720},
        }],
    }
    client.get.side_effect = [_error_response(404), vx_resp]  # fxtwitter 404 -> vxtwitter
    result = await resolve("https://twitter.com/cars/status/9", "twitter", client=client)
    assert result.via == "vxtwitter_api"
    assert result.video_url == "https://video.twimg.com/ext_tw_video/9/pu/vid/avc1/1280x720/v.mp4"
    assert result.image_url == "https://pbs.twimg.com/ext_tw_video_thumb/9/pu/img/still.jpg"
    assert result.video_width == 1280  # noqa: PLR2004
    assert result.video_height == 720  # noqa: PLR2004


@pytest.mark.asyncio
async def test_resolve_tiktok_tikwm_returns_play_url():
    client = AsyncMock()
    tikwm_resp = MagicMock()
    tikwm_resp.status_code = 200
    tikwm_resp.json = MagicMock(return_value={
        "code": 0,
        "data": {
            "cover": "https://cdn.tiktok.com/cover.jpg",
            "play": "https://v16m.tiktokcdn-us.com/abc/video.mp4",
            "author": {"nickname": "Someone"},
        },
    })
    client.get.return_value = tikwm_resp
    result = await resolve("https://www.tiktok.com/@someone/video/1", "tiktok", client=client)
    assert result.video_url == "https://v16m.tiktokcdn-us.com/abc/video.mp4"
    assert result.image_url == "https://cdn.tiktok.com/cover.jpg"


@pytest.mark.asyncio
async def test_resolve_reddit_gif_returns_video_url():
    client = AsyncMock()
    reddit_resp = MagicMock()
    reddit_resp.status_code = 200
    reddit_resp.json.return_value = [
        {"data": {"children": [{"data": {
            "title": "cool gif",
            "subreddit_name_prefixed": "r/robots",
            "url": "https://v.redd.it/xyz",
            "secure_media": {"reddit_video": {
                "fallback_url": "https://v.redd.it/xyz/DASH_480.mp4?source=fallback",
                "width": 480, "height": 480, "is_gif": True,
            }},
            "preview": {"images": [{"source": {"url": "https://preview.redd.it/still.jpg"}}]},
        }}]}}
    ]
    client.get.return_value = reddit_resp
    result = await resolve("https://www.reddit.com/r/robots/comments/abc/cool_gif/", "reddit", client=client)
    # GIFs have no audio: the video-only fallback_url is downloaded directly.
    assert result.video_url == "https://v.redd.it/xyz/DASH_480.mp4?source=fallback"
    assert result.video_is_stream is False
    assert result.video_width == 480  # noqa: PLR2004


@pytest.mark.asyncio
async def test_resolve_reddit_regular_video_uses_stream_manifest():
    # Regular videos have separate audio, so we hand the HLS manifest to ffmpeg to mux.
    client = AsyncMock()
    reddit_resp = MagicMock()
    reddit_resp.status_code = 200
    reddit_resp.json.return_value = [
        {"data": {"children": [{"data": {
            "title": "video with sound",
            "subreddit_name_prefixed": "r/robots",
            "url": "https://v.redd.it/abc",
            "secure_media": {"reddit_video": {
                "fallback_url": "https://v.redd.it/abc/DASH_1080.mp4?source=fallback",
                "hls_url": "https://v.redd.it/abc/HLSPlaylist.m3u8?token=xyz",
                "dash_url": "https://v.redd.it/abc/DASHPlaylist.mpd?token=xyz",
                "width": 1920, "height": 1080, "is_gif": False,
            }},
            "preview": {"images": [{"source": {"url": "https://preview.redd.it/still.jpg"}}]},
        }}]}}
    ]
    client.get.return_value = reddit_resp
    result = await resolve("https://www.reddit.com/r/robots/comments/def/video/", "reddit", client=client)
    assert result.video_url == "https://v.redd.it/abc/HLSPlaylist.m3u8?token=xyz"  # HLS preferred
    assert result.video_is_stream is True
    assert result.image_url == "https://preview.redd.it/still.jpg"


@pytest.mark.asyncio
async def test_resolve_reddit_regular_video_without_manifest_is_skipped():
    # No HLS/DASH manifest -> a bare fallback_url would be silent, so skip the video.
    client = AsyncMock()
    reddit_resp = MagicMock()
    reddit_resp.status_code = 200
    reddit_resp.json.return_value = [
        {"data": {"children": [{"data": {
            "title": "video no manifest",
            "subreddit_name_prefixed": "r/robots",
            "url": "https://v.redd.it/abc",
            "secure_media": {"reddit_video": {
                "fallback_url": "https://v.redd.it/abc/DASH_1080.mp4?source=fallback",
                "width": 1920, "height": 1080, "is_gif": False,
            }},
            "preview": {"images": [{"source": {"url": "https://preview.redd.it/still.jpg"}}]},
        }}]}}
    ]
    client.get.return_value = reddit_resp
    result = await resolve("https://www.reddit.com/r/robots/comments/def/video/", "reddit", client=client)
    assert result.video_url is None
    assert result.image_url == "https://preview.redd.it/still.jpg"


# --- service._resolve_links thumbnail download (wikimedia headers + discord proxy fallback) ---
#
# These live here for topic proximity (they exercise how resolved metadata is
# turned into a downloaded thumbnail) but the code under test is
# bot.discord_ingest.service._resolve_links. resolve() and download_attachment
# are patched at the service module level.

import httpx

from bot.discord_ingest.service import _resolve_links
from bot.models import SubmissionLink
from bot.resolve.fetch import ResolvedMetadata, _UA

from conftest import make_submission


def _link_settings(tmp_path) -> MagicMock:
    s = MagicMock()
    s.attachments_dir = str(tmp_path / "attachments")
    s.data_dir = str(tmp_path)
    s.storage_min_free_mb = 0
    s.youtube_api_key = None
    return s


async def _seed_link(session, board, url: str, family: str = "other"):
    """Create a submission holding a single unresolved link; returns (submission, link)."""
    sub = make_submission(board)
    session.add(sub)
    await session.flush()
    link = SubmissionLink(
        submission_id=sub.id,
        order_index=0,
        raw_url=url,
        canonical_url=url,
        domain_family=family,
    )
    session.add(link)
    await session.flush()
    return sub, link


@pytest.mark.asyncio
async def test_resolve_links_wikimedia_image_gets_referer_and_ua(session, board, tmp_path):
    """upload.wikimedia.org thumbnails are fetched with a Referer + resolver UA
    (their CDN 403s bare requests).
    """
    submission, link = await _seed_link(session, board, "https://en.wikipedia.org/wiki/Oshkosh_NGDV", "wikipedia")
    meta = ResolvedMetadata(
        title="Oshkosh NGDV",
        image_url="https://upload.wikimedia.org/wikipedia/commons/thumb/d/de/USPS.jpg",
        via="wikipedia_api",
    )

    with patch("bot.discord_ingest.service.resolve", new=AsyncMock(return_value=meta)), \
         patch("bot.discord_ingest.service.download_attachment", new=AsyncMock(return_value="/vol/thumb_1")) as dl:
        await _resolve_links(session, submission, _link_settings(tmp_path), MagicMock())

    dl.assert_called_once()
    headers = dl.call_args.kwargs["headers"]
    assert headers["Referer"] == "https://en.wikipedia.org/"
    assert headers["User-Agent"] == _UA
    assert link.resolved_image_path == "/vol/thumb_1"


@pytest.mark.asyncio
async def test_resolve_links_non_wikimedia_image_gets_no_extra_headers(session, board, tmp_path):
    submission, link = await _seed_link(session, board, "https://example.com/post")
    meta = ResolvedMetadata(title="Post", image_url="https://cdn.example.com/img.jpg", via="opengraph")

    with patch("bot.discord_ingest.service.resolve", new=AsyncMock(return_value=meta)), \
         patch("bot.discord_ingest.service.download_attachment", new=AsyncMock(return_value="/vol/thumb_2")) as dl:
        await _resolve_links(session, submission, _link_settings(tmp_path), MagicMock())

    dl.assert_called_once()
    assert dl.call_args.kwargs["headers"] is None


@pytest.mark.asyncio
async def test_resolve_links_falls_back_to_discord_proxy(session, board, tmp_path):
    """Primary thumbnail download fails: the Discord CDN proxy copy is tried
    and its path recorded.
    """
    submission, link = await _seed_link(session, board, "https://www.furaffinity.net/view/123/")
    meta = ResolvedMetadata(title="Art", image_url="https://d.furaffinity.net/art/x.png", via="opengraph")
    proxy = "https://media.discordapp.net/external/proxy.png"

    dl = AsyncMock(side_effect=[httpx.HTTPError("403 blocked"), "/vol/thumb_proxy"])
    with patch("bot.discord_ingest.service.resolve", new=AsyncMock(return_value=meta)), \
         patch("bot.discord_ingest.service.download_attachment", new=dl):
        await _resolve_links(
            session, submission, _link_settings(tmp_path), MagicMock(),
            embed_thumb_proxy_url=proxy,
        )

    assert dl.call_count == 2
    assert dl.call_args_list[0].kwargs["url"] == meta.image_url
    assert dl.call_args_list[1].kwargs["url"] == proxy
    assert link.resolved_image_path == "/vol/thumb_proxy"


@pytest.mark.asyncio
async def test_resolve_links_proxy_failure_swallowed(session, board, tmp_path):
    """Both the source CDN and the Discord proxy failing is non-fatal:
    metadata is kept, resolved_image_path just stays unset.
    """
    submission, link = await _seed_link(session, board, "https://www.furaffinity.net/view/456/")
    meta = ResolvedMetadata(title="Art", image_url="https://d.furaffinity.net/art/y.png", via="opengraph")

    dl = AsyncMock(side_effect=[httpx.HTTPError("403"), httpx.HTTPError("404")])
    with patch("bot.discord_ingest.service.resolve", new=AsyncMock(return_value=meta)), \
         patch("bot.discord_ingest.service.download_attachment", new=dl):
        await _resolve_links(
            session, submission, _link_settings(tmp_path), MagicMock(),
            embed_thumb_proxy_url="https://media.discordapp.net/external/other.png",
        )

    assert dl.call_count == 2
    assert link.resolved_image_path is None
    assert link.resolved_title == "Art"


@pytest.mark.asyncio
async def test_resolve_links_no_proxy_url_single_attempt(session, board, tmp_path):
    """Download failure with no Discord proxy available: exactly one attempt."""
    submission, link = await _seed_link(session, board, "https://example.com/no-proxy")
    meta = ResolvedMetadata(title="Post", image_url="https://cdn.example.com/z.jpg", via="opengraph")

    dl = AsyncMock(side_effect=httpx.HTTPError("boom"))
    with patch("bot.discord_ingest.service.resolve", new=AsyncMock(return_value=meta)), \
         patch("bot.discord_ingest.service.download_attachment", new=dl):
        await _resolve_links(session, submission, _link_settings(tmp_path), MagicMock())

    assert dl.call_count == 1
    assert link.resolved_image_path is None


# ---------------------------------------------------------------------------
# youtube api, wikipedia, reddit branches, mirrors, and resolve() fallbacks
# ---------------------------------------------------------------------------

from bot.resolve.fetch import (
    _youtube_video_id,
    _youtube_watch_url,
    _deviantart_mirror_url,
    _reddit_image_url,
)


def _json_response(payload, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.json = MagicMock(return_value=payload)
    return resp


def test_youtube_video_id_variants():
    assert _youtube_video_id("https://youtu.be/abc123") == "abc123"
    assert _youtube_video_id("https://www.youtube.com/watch?v=xyz") == "xyz"
    assert _youtube_video_id("https://youtu.be/") is None
    assert _youtube_video_id("https://example.com/watch?v=xyz") is None


def test_youtube_watch_url_conversion():
    assert _youtube_watch_url("https://youtu.be/abc") == "https://www.youtube.com/watch?v=abc"
    assert _youtube_watch_url("https://youtu.be/") == "https://youtu.be/"
    assert _youtube_watch_url("https://www.youtube.com/watch?v=abc") == "https://www.youtube.com/watch?v=abc"


def test_deviantart_mirror_url():
    assert "fixdeviantart.com" in _deviantart_mirror_url("https://www.deviantart.com/a/art/B-1")


@pytest.mark.asyncio
async def test_resolve_youtube_api_used_when_key_present():
    client = AsyncMock()
    client.get.return_value = _json_response({
        "items": [{"snippet": {
            "title": "Video Title",
            "channelTitle": "Channel",
            "thumbnails": {"maxres": {"url": "https://i.ytimg.com/vi/x/maxresdefault.jpg"}},
        }}]
    })
    result = await resolve(
        "https://www.youtube.com/watch?v=x", "youtube",
        client=client, youtube_api_key="KEY",
    )
    assert result.title == "Video Title"
    assert result.via == "youtube_api"
    assert "googleapis.com" in client.get.call_args_list[0][0][0]


@pytest.mark.asyncio
async def test_resolve_youtube_api_non_200_falls_to_oembed():
    client = AsyncMock()
    client.get.side_effect = [
        _json_response({}, status=403),               # data api quota
        _oembed_response("Fallback Title"),           # oembed
    ]
    result = await resolve(
        "https://www.youtube.com/watch?v=x", "youtube",
        client=client, youtube_api_key="KEY",
    )
    assert result.title == "Fallback Title"
    assert result.via == "oembed"


@pytest.mark.asyncio
async def test_resolve_youtube_api_empty_items_falls_through():
    client = AsyncMock()
    client.get.side_effect = [
        _json_response({"items": []}),
        _oembed_response("OEmbed Title"),
    ]
    result = await resolve(
        "https://www.youtube.com/watch?v=x", "youtube",
        client=client, youtube_api_key="KEY",
    )
    assert result.via == "oembed"


@pytest.mark.asyncio
async def test_resolve_youtube_api_thumbnail_priority():
    client = AsyncMock()
    client.get.return_value = _json_response({
        "items": [{"snippet": {
            "title": "T", "channelTitle": "C",
            "thumbnails": {"default": {"url": "https://i.ytimg.com/vi/x/default.jpg"},
                           "high": {"url": "https://i.ytimg.com/vi/x/hq.jpg"}},
        }}]
    })
    result = await resolve("https://youtu.be/x", "youtube", client=client, youtube_api_key="KEY")
    assert result.image_url == "https://i.ytimg.com/vi/x/hq.jpg"  # high beats default


@pytest.mark.asyncio
async def test_resolve_wikipedia_api():
    client = AsyncMock()
    client.get.return_value = _json_response({
        "title": "Robot",
        "description": "mechanical being",
        "thumbnail": {"source": "https://upload.wikimedia.org/robot.jpg"},
    })
    result = await resolve("https://en.wikipedia.org/wiki/Robot", "wikipedia", client=client)
    assert result.title == "Robot"
    assert result.via == "wikipedia_api"
    assert "api/rest_v1/page/summary/Robot" in client.get.call_args_list[0][0][0]


@pytest.mark.asyncio
async def test_resolve_wikipedia_bad_path_falls_to_opengraph():
    client = AsyncMock()
    client.get.return_value = _html_response("Wiki OG")
    result = await resolve("https://en.wikipedia.org/notwiki", "wikipedia", client=client)
    assert result.title == "Wiki OG"


@pytest.mark.asyncio
async def test_resolve_wikipedia_non_200_and_parse_error():
    client = AsyncMock()
    bad_json = MagicMock()
    bad_json.status_code = 200
    bad_json.json = MagicMock(side_effect=ValueError("nope"))
    bad_json.headers = {"content-type": "text/html"}
    bad_json.text = "<html><head><title>fallback</title></head></html>"
    bad_json.url = "https://en.wikipedia.org/wiki/X"
    bad_json.raise_for_status = MagicMock()
    client.get.side_effect = [_json_response({}, status=503), bad_json]
    result = await resolve("https://en.wikipedia.org/wiki/X", "wikipedia", client=client)
    # api 503 -> opengraph fallback fetch of the canonical page
    assert result.via in ("html", "none", "opengraph")


def test_reddit_image_url_direct_and_extension():
    assert _reddit_image_url({"url": "https://i.redd.it/abc.jpg"}) == "https://i.redd.it/abc.jpg"
    assert _reddit_image_url({"url": "https://cdn.example.com/pic.PNG"}) == "https://cdn.example.com/pic.PNG"
    assert _reddit_image_url({"url": "https://example.com/page", "preview": {
        "images": [{"source": {"url": "https://preview.redd.it/x?a=1&amp;b=2"}}]
    }}) == "https://preview.redd.it/x?a=1&b=2"
    assert _reddit_image_url({"url": "https://example.com/page"}) is None


@pytest.mark.asyncio
async def test_resolve_reddit_unexpected_shape_falls_to_mirror():
    client = AsyncMock()
    client.get.side_effect = [
        _json_response({"unexpected": True}),   # json api wrong shape
        _html_response("vxreddit OG"),          # vxreddit mirror
    ]
    result = await resolve("https://www.reddit.com/r/x/comments/1/t/", "reddit", client=client)
    assert result.title == "vxreddit OG"
    assert "vxreddit.com" in client.get.call_args_list[1][0][0]


@pytest.mark.asyncio
async def test_resolve_reddit_crosspost_parent_image():
    client = AsyncMock()
    client.get.return_value = _json_response([
        {"data": {"children": [{"data": {
            "title": "xpost", "subreddit_name_prefixed": "r/x",
            "url": "https://example.com/page",
            "crosspost_parent_list": [{"url": "https://i.redd.it/orig.jpg"}],
        }}]}}
    ])
    result = await resolve("https://www.reddit.com/r/x/comments/1/t/", "reddit", client=client)
    assert result.image_url == "https://i.redd.it/orig.jpg"


@pytest.mark.asyncio
async def test_resolve_opengraph_non_html_content_type():
    client = AsyncMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {"content-type": "application/pdf"}
    resp.raise_for_status = MagicMock()
    client.get.return_value = resp
    result = await resolve("https://example.com/doc.pdf", "other", client=client)
    assert result.via == "none"


@pytest.mark.asyncio
async def test_resolve_falls_back_to_discord_embed_values():
    client = AsyncMock()
    client.get.side_effect = httpx.ConnectError("down")
    result = await resolve(
        "https://example.com/x", "other", client=client,
        fallback_title="Discord Title", fallback_image_url="https://cdn.discord.com/t.jpg",
    )
    assert result.title == "Discord Title"
    assert result.via == "discord"


@pytest.mark.asyncio
async def test_resolve_nothing_anywhere_is_none():
    client = AsyncMock()
    client.get.side_effect = httpx.ConnectError("down")
    result = await resolve("https://example.com/x", "other", client=client)
    assert result.via == "none"
    assert result.title is None


@pytest.mark.asyncio
async def test_resolve_tiktok_http_error_and_bad_json():
    client = AsyncMock()
    client.get.side_effect = [httpx.ConnectError("down"), _html_response("TikTok Page")]
    result = await resolve("https://www.tiktok.com/@u/video/1", "tiktok", client=client)
    assert result.title == "TikTok Page"

    client2 = AsyncMock()
    bad = MagicMock()
    bad.status_code = 200
    bad.json = MagicMock(side_effect=ValueError)
    client2.get.side_effect = [bad, _html_response("TikTok Page 2")]
    result2 = await resolve("https://www.tiktok.com/@u/video/2", "tiktok", client=client2)
    assert result2.title == "TikTok Page 2"


@pytest.mark.asyncio
async def test_resolve_tikwm_no_cover_no_author_falls_through():
    client = AsyncMock()
    client.get.side_effect = [_json_response({"data": {}}), _html_response("TT OG")]
    result = await resolve("https://www.tiktok.com/@u/video/3", "tiktok", client=client)
    assert result.title == "TT OG"


@pytest.mark.asyncio
async def test_resolve_deviantart_thumbnail_fallback_to_url():
    client = AsyncMock()
    client.get.return_value = _json_response({
        "title": "Art", "author_name": "Artist", "url": "https://cdn.da.com/full.png",
    })
    result = await resolve("https://www.deviantart.com/a/art/B-1", "deviantart", client=client)
    assert result.image_url == "https://cdn.da.com/full.png"


@pytest.mark.asyncio
async def test_resolve_youtube_api_no_video_id_falls_to_oembed():
    client = AsyncMock()
    client.get.return_value = _oembed_response("Playlist Page")
    # A playlist URL has no v= parameter, so the data API step is skipped entirely.
    result = await resolve(
        "https://www.youtube.com/playlist?list=PLx", "youtube",
        client=client, youtube_api_key="KEY",
    )
    assert result.via == "oembed"
    assert "oembed" in client.get.call_args_list[0][0][0]


@pytest.mark.asyncio
async def test_resolve_youtube_oembed_non_200_falls_to_watch_page():
    client = AsyncMock()
    client.get.side_effect = [_error_response(401), _html_response("Watch Page")]
    result = await resolve("https://www.youtube.com/watch?v=x", "youtube", client=client)
    assert result.title == "Watch Page"


@pytest.mark.asyncio
async def test_resolve_instagram_oembed_bad_json_falls_through():
    client = AsyncMock()
    bad = MagicMock()
    bad.status_code = 200
    bad.json = MagicMock(side_effect=ValueError)
    client.get.side_effect = [bad, _html_response("IG Mirror")]
    result = await resolve("https://www.instagram.com/p/abc/", "instagram", client=client)
    assert result.title == "IG Mirror"
    assert "kkinstagram.com" in client.get.call_args_list[1][0][0]


@pytest.mark.asyncio
async def test_resolve_twitter_non_status_url_skips_apis():
    client = AsyncMock()
    client.get.return_value = _html_response("Profile Page")
    result = await resolve("https://twitter.com/someuser", "twitter", client=client)
    # No /status/ segment: both APIs skip, goes straight to fxtwitter mirror OG.
    assert result.title == "Profile Page"
    assert "fxtwitter.com" in client.get.call_args_list[0][0][0]


@pytest.mark.asyncio
async def test_resolve_vxtwitter_image_only_tweet():
    client = AsyncMock()
    vx = _json_response({
        "text": "pic tweet", "user_name": "U",
        "mediaURLs": ["https://pbs.twimg.com/media/a.jpg"],
        "media_extended": [{"type": "image", "url": "https://pbs.twimg.com/media/a.jpg"}],
    })
    client.get.side_effect = [_error_response(404), vx]
    result = await resolve("https://twitter.com/u/status/5", "twitter", client=client)
    assert result.image_url == "https://pbs.twimg.com/media/a.jpg"
    assert result.video_url is None


@pytest.mark.asyncio
async def test_resolve_tikwm_non_200_falls_to_direct():
    client = AsyncMock()
    client.get.side_effect = [_error_response(429), _html_response("TT Direct")]
    result = await resolve("https://www.tiktok.com/@u/video/9", "tiktok", client=client)
    assert result.title == "TT Direct"


def test_reddit_image_url_preview_without_url_key():
    # source dict present but url empty -> falls through to None
    assert _reddit_image_url({"url": "", "preview": {"images": [{"source": {}}]}}) is None


@pytest.mark.asyncio
async def test_resolve_reddit_crosspost_parent_gif_video():
    client = AsyncMock()
    client.get.return_value = _json_response([
        {"data": {"children": [{"data": {
            "title": "xpost gif", "subreddit_name_prefixed": "r/x",
            "url": "https://example.com/page",
            "crosspost_parent_list": [{
                "url": "https://i.redd.it/orig.jpg",
                "secure_media": {"reddit_video": {
                    "fallback_url": "https://v.redd.it/g/DASH_480.mp4",
                    "width": 480, "height": 320, "is_gif": True,
                }},
            }],
        }}]}}
    ])
    result = await resolve("https://www.reddit.com/r/x/comments/2/t/", "reddit", client=client)
    assert result.video_url == "https://v.redd.it/g/DASH_480.mp4"


@pytest.mark.asyncio
async def test_resolve_mirror_opengraph_error_falls_to_canonical():
    client = AsyncMock()
    client.get.side_effect = [
        _error_response(404),        # reddit json api
        httpx.ConnectError("down"),  # vxreddit mirror raises
        _html_response("Canonical"), # canonical fetch works
    ]
    result = await resolve("https://www.reddit.com/r/x/comments/3/t/", "reddit", client=client)
    assert result.title == "Canonical"


def test_meta_parser_ignores_meta_without_content():
    from bot.resolve.fetch import parse_html_metadata
    html = '<html><head><meta property="og:title"><title>T</title></head></html>'
    meta = parse_html_metadata(html, "https://example.com")
    assert meta.title == "T"
    assert meta.via == "html"


@pytest.mark.asyncio
async def test_resolve_vxtwitter_media_urls_fallback_when_no_media_extended():
    client = AsyncMock()
    vx = _json_response({
        "text": "old-format tweet", "user_name": "U",
        "mediaURLs": ["https://pbs.twimg.com/media/only.jpg"],
        "media_extended": [],
    })
    client.get.side_effect = [_error_response(404), vx]
    result = await resolve("https://twitter.com/u/status/6", "twitter", client=client)
    assert result.image_url == "https://pbs.twimg.com/media/only.jpg"


@pytest.mark.asyncio
async def test_resolve_wikipedia_request_error_falls_through():
    client = AsyncMock()
    client.get.side_effect = [httpx.ConnectError("down"), _html_response("Wiki Direct")]
    result = await resolve("https://en.wikipedia.org/wiki/Robot", "wikipedia", client=client)
    assert result.title == "Wiki Direct"


@pytest.mark.asyncio
async def test_resolve_wikipedia_json_error_falls_through():
    client = AsyncMock()
    bad = MagicMock()
    bad.status_code = 200
    bad.json = MagicMock(side_effect=ValueError)
    client.get.side_effect = [bad, _html_response("Wiki Direct 2")]
    result = await resolve("https://en.wikipedia.org/wiki/Robot", "wikipedia", client=client)
    assert result.title == "Wiki Direct 2"


@pytest.mark.asyncio
async def test_resolve_reddit_request_error_falls_to_mirror():
    client = AsyncMock()
    client.get.side_effect = [httpx.ConnectError("down"), _html_response("vxreddit")]
    result = await resolve("https://www.reddit.com/r/x/comments/4/t/", "reddit", client=client)
    assert result.title == "vxreddit"


@pytest.mark.asyncio
async def test_resolve_reddit_crosspost_parent_not_needed_when_post_has_both():
    client = AsyncMock()
    client.get.return_value = _json_response([
        {"data": {"children": [{"data": {
            "title": "self-sufficient", "subreddit_name_prefixed": "r/x",
            "url": "https://i.redd.it/own.jpg",
            "secure_media": {"reddit_video": {
                "fallback_url": "https://v.redd.it/own/DASH_480.mp4",
                "width": 1, "height": 1, "is_gif": True,
            }},
            "crosspost_parent_list": [{"url": "https://i.redd.it/parent.jpg"}],
        }}]}}
    ])
    result = await resolve("https://www.reddit.com/r/x/comments/5/t/", "reddit", client=client)
    assert result.image_url == "https://i.redd.it/own.jpg"
    assert result.video_url == "https://v.redd.it/own/DASH_480.mp4"


@pytest.mark.asyncio
async def test_resolve_youtube_api_raising_is_caught_by_resolve():
    client = AsyncMock()
    client.get.side_effect = [httpx.ConnectError("api down"), _oembed_response("After API Error")]
    result = await resolve(
        "https://www.youtube.com/watch?v=x", "youtube",
        client=client, youtube_api_key="KEY",
    )
    assert result.title == "After API Error"


@pytest.mark.asyncio
async def test_resolve_oembed_handler_raising_is_caught_by_resolve():
    client = AsyncMock()
    # youtube oembed handler does not catch network errors itself
    client.get.side_effect = [httpx.ConnectError("oembed down"), _html_response("OG After")]
    result = await resolve("https://www.youtube.com/watch?v=x", "youtube", client=client)
    assert result.title == "OG After"


@pytest.mark.asyncio
async def test_resolve_fxtwitter_200_with_empty_tweet_falls_to_vxtwitter():
    client = AsyncMock()
    client.get.side_effect = [
        _json_response({"tweet": {}}),  # 200 but empty payload
        _json_response({"text": "vx", "user_name": "U", "mediaURLs": [], "media_extended": []}),
    ]
    result = await resolve("https://twitter.com/u/status/7", "twitter", client=client)
    assert result.via == "vxtwitter_api"


@pytest.mark.asyncio
async def test_resolve_vxtwitter_unknown_media_type_ignored():
    client = AsyncMock()
    vx = _json_response({
        "text": "poll tweet", "user_name": "U",
        "mediaURLs": [],
        "media_extended": [{"type": "poll", "url": "https://x/poll"},
                           {"type": "image", "url": "https://pbs.twimg.com/media/i.jpg"}],
    })
    client.get.side_effect = [_error_response(404), vx]
    result = await resolve("https://twitter.com/u/status/8", "twitter", client=client)
    assert result.image_url == "https://pbs.twimg.com/media/i.jpg"


# --- reddit mirror (vxreddit) og:video muxed-mp4 path ------------------------

from bot.resolve.fetch import _extract_og_video, _reddit_mirror


def test_extract_og_video():
    assert _extract_og_video(
        '<meta property="og:video:secure_url" content="https://x/v.mp4?a=1&amp;b=2">'
    ) == "https://x/v.mp4?a=1&b=2"
    assert _extract_og_video('<meta property="og:video" content="https://x/w.mp4">') == "https://x/w.mp4"
    assert _extract_og_video("<html><head><title>no video</title></head></html>") is None


def _reddit_mirror_resp(video_url, title="Reddit Vid", image="https://preview.redd.it/s.jpg"):
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {"content-type": "text/html; charset=utf-8"}
    resp.text = (
        "<html><head>"
        f'<meta property="og:title" content="{title}">'
        f'<meta property="og:image" content="{image}">'
        f'<meta property="og:video:secure_url" content="{video_url}">'
        "</head></html>"
    )
    resp.url = "https://vxreddit.com/r/mallninjashit/comments/1sz144x/"
    resp.raise_for_status = MagicMock()
    return resp


@pytest.mark.asyncio
async def test_resolve_reddit_json_403_uses_mirror_muxed_video():
    # reddit.com JSON blocks the host (403); the vxreddit mirror serves a muxed mp4.
    client = AsyncMock()
    vid = "https://vxreddit.com/redditvideo.mp4?video_url=https%3A%2F%2Fv.redd.it%2Fabc%2FCMAF_720.m3u8&amp;audio_url=y"
    client.get.side_effect = [_error_response(403), _reddit_mirror_resp(vid)]
    result = await resolve("https://www.reddit.com/r/mallninjashit/comments/1sz144x/", "reddit", client=client)
    assert result.via == "reddit_mirror"
    assert result.video_url == (
        "https://vxreddit.com/redditvideo.mp4?video_url=https%3A%2F%2Fv.redd.it%2Fabc%2FCMAF_720.m3u8&audio_url=y"
    )
    assert result.video_is_stream is False  # a directly downloadable muxed mp4
    assert result.image_url == "https://preview.redd.it/s.jpg"
    assert "vxreddit.com" in client.get.call_args_list[1][0][0]


@pytest.mark.asyncio
async def test_resolve_reddit_json_success_skips_mirror():
    # When reddit.com JSON works, it is trusted whole - the mirror is not consulted.
    client = AsyncMock()
    client.get.return_value = _json_response([
        {"data": {"children": [{"data": {
            "title": "img post", "subreddit_name_prefixed": "r/x",
            "url": "https://i.redd.it/pic.jpg",
        }}]}}
    ])
    result = await resolve("https://www.reddit.com/r/x/comments/1/t/", "reddit", client=client)
    assert result.image_url == "https://i.redd.it/pic.jpg"
    assert client.get.call_count == 1  # no mirror fetch


@pytest.mark.asyncio
async def test_reddit_mirror_non_200_returns_none():
    client = AsyncMock()
    client.get.return_value = _error_response(502)
    assert await _reddit_mirror("https://www.reddit.com/r/x/comments/1/t/", client) is None


@pytest.mark.asyncio
async def test_reddit_mirror_http_error_returns_none():
    client = AsyncMock()
    client.get.side_effect = httpx.ConnectError("down")
    assert await _reddit_mirror("https://www.reddit.com/r/x/comments/1/t/", client) is None


@pytest.mark.asyncio
async def test_reddit_mirror_unrewritable_url_returns_none():
    # A URL that _reddit_mirror_url can't rewrite (no www.reddit.com prefix) -> no mirror.
    client = AsyncMock()
    result = await _reddit_mirror("https://old.reddit.com/r/x/comments/1/t/", client)
    assert result is None
    client.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_reddit_mirror_uses_crawler_user_agent():
    # vxreddit only serves its embed page to a recognised crawler UA; a plain UA is
    # bounced to reddit (403 on our IP). The mirror fetch must send the crawler token.
    from bot.resolve.fetch import _CRAWLER_HEADERS
    client = AsyncMock()
    vid = "https://vxreddit.com/redditvideo.mp4?video_url=https%3A%2F%2Fv.redd.it%2Fabc%2F"
    client.get.return_value = _reddit_mirror_resp(vid)
    await _reddit_mirror("https://www.reddit.com/r/x/comments/1/t/", client)
    sent_headers = client.get.call_args.kwargs["headers"]
    assert sent_headers is _CRAWLER_HEADERS
    assert "Discordbot" in sent_headers["User-Agent"]
    assert "ExegesisRepostBot" in sent_headers["User-Agent"]  # still identifies us
