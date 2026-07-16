"""Fetch + parse source metadata. No third-party HTML deps (stdlib html.parser)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import quote, urljoin

import httpx

log = logging.getLogger(__name__)

_UA = "Mozilla/5.0 (compatible; ExegesisRepostBot/0.1; +https://bsky.app)"
_HEADERS = {"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml"}

# Embed-mirror services (vxreddit and friends) serve their OpenGraph embed page only
# to recognised link-unfurl crawlers; a plain UA gets a 302 bounce to the real site
# (which 403s our datacenter IP). We still identify ourselves honestly, but append the
# Discordbot crawler token these services whitelist so they hand us the embed.
_CRAWLER_UA = "Mozilla/5.0 (compatible; ExegesisRepostBot/0.1; +https://bsky.app; Discordbot/2.0)"
_CRAWLER_HEADERS = {"User-Agent": _CRAWLER_UA, "Accept": "text/html,application/xhtml+xml"}
_FETCH_TIMEOUT = 15.0
_MAX_HTML_BYTES = 2_000_000  # don't parse arbitrarily large bodies


@dataclass
class ResolvedMetadata:
    title: str | None = None
    description: str | None = None
    image_url: str | None = None
    # oembed | opengraph | html | discord | none | skipped | unavailable.
    # "unavailable" = the source exists but can't be fetched without an authenticated,
    # age-verified session (e.g. an age-restricted twitter tweet); callers surface a
    # clear "attach the media yourself" notice rather than a broken empty card.
    via: str = "none"
    # Video URL when the source is a video/GIF post and the resolver exposes one
    # (fxtwitter, vxtwitter, tikwm, reddit). image_url still carries the thumbnail
    # still for fallback/preview use.
    video_url: str | None = None
    video_width: int | None = None
    video_height: int | None = None
    # True when video_url is a stream manifest (HLS/DASH) that ffmpeg must fetch and
    # mux (e.g. reddit, where video and audio are separate streams) rather than a
    # single downloadable file.
    video_is_stream: bool = False


class _MetaParser(HTMLParser):
    """Collect <title> text and OpenGraph/Twitter/<meta name> tags from <head>."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.metas: dict[str, str] = {}
        self.title: str | None = None
        self._in_title = False
        self._title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            a = {k.lower(): (v or "") for k, v in attrs}
            key = a.get("property") or a.get("name")
            content = a.get("content")
            if key and content and key.lower() not in self.metas:
                self.metas[key.lower()] = content

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)

    def finish(self) -> None:
        if self._title_parts:
            self.title = "".join(self._title_parts).strip() or None


def _first(metas: dict[str, str], *keys: str) -> str | None:
    for k in keys:
        if metas.get(k):
            return metas[k].strip()
    return None


def parse_html_metadata(html: str, base_url: str) -> ResolvedMetadata:
    """Extract title/description/image from an HTML document (head metadata)."""
    parser = _MetaParser()
    parser.feed(html)
    parser.finish()
    metas = parser.metas

    title = _first(metas, "og:title", "twitter:title") or parser.title
    description = _first(metas, "og:description", "twitter:description", "description")
    image = _first(metas, "og:image", "og:image:url", "twitter:image", "twitter:image:src")
    if image:
        image = urljoin(base_url, image)  # resolve relative image URLs

    if _first(metas, "og:title", "og:image", "og:description"):
        via = "opengraph"
    elif title:
        via = "html"
    else:
        via = "none"
    return ResolvedMetadata(title=title, description=description, image_url=image, via=via)


def _youtube_video_id(url: str) -> str | None:
    from urllib.parse import urlparse, parse_qs
    parsed = urlparse(url)
    if parsed.netloc == "youtu.be":
        return parsed.path.lstrip("/").split("/")[0] or None
    if parsed.netloc in ("www.youtube.com", "youtube.com"):
        return parse_qs(parsed.query).get("v", [None])[0]
    return None


async def _youtube_api(url: str, client: httpx.AsyncClient, api_key: str) -> ResolvedMetadata | None:
    """Fetch YouTube video metadata via the Data API v3 (1 quota unit per call).

    Works from datacenter IPs where scraping returns a JS-only shell page.
    Falls back gracefully to None if the video ID can't be extracted or the
    API returns an error (e.g. the key is invalid or quota is exhausted).
    """
    video_id = _youtube_video_id(url)
    if not video_id:
        return None
    endpoint = (
        f"https://www.googleapis.com/youtube/v3/videos"
        f"?id={video_id}&part=snippet&key={api_key}"
    )
    resp = await client.get(endpoint, timeout=_FETCH_TIMEOUT)
    if resp.status_code != 200:
        log.info("youtube data api returned %d for %s", resp.status_code, url)
        return None
    items = resp.json().get("items", [])
    if not items:
        return None
    snippet = items[0].get("snippet", {})
    thumbs = snippet.get("thumbnails", {})
    thumb = (
        thumbs.get("maxres") or thumbs.get("standard")
        or thumbs.get("high") or thumbs.get("medium")
        or thumbs.get("default") or {}
    )
    return ResolvedMetadata(
        title=snippet.get("title"),
        description=snippet.get("channelTitle"),
        image_url=thumb.get("url"),
        via="youtube_api",
    )


async def _youtube_oembed(url: str, client: httpx.AsyncClient) -> ResolvedMetadata | None:
    endpoint = f"https://www.youtube.com/oembed?format=json&url={quote(url, safe='')}"
    resp = await client.get(endpoint, headers=_HEADERS, timeout=_FETCH_TIMEOUT)
    if resp.status_code != 200:
        log.info("youtube oembed returned %d for %s", resp.status_code, url)
        return None
    data = resp.json()
    return ResolvedMetadata(
        title=data.get("title"),
        description=data.get("author_name"),
        image_url=data.get("thumbnail_url"),
        via="oembed",
    )


async def _deviantart_oembed(url: str, client: httpx.AsyncClient) -> ResolvedMetadata | None:
    endpoint = f"https://backend.deviantart.com/oembed?url={quote(url, safe='')}&format=json"
    resp = await client.get(endpoint, headers=_HEADERS, timeout=_FETCH_TIMEOUT)
    if resp.status_code != 200:
        return None
    data = resp.json()
    return ResolvedMetadata(
        title=data.get("title"),
        description=data.get("author_name"),
        image_url=data.get("thumbnail_url") or data.get("url"),
        via="oembed",
    )


async def _instagram_oembed(url: str, client: httpx.AsyncClient) -> ResolvedMetadata | None:
    endpoint = f"https://www.instagram.com/api/v1/oembed/?url={quote(url, safe='')}&format=json"
    resp = await client.get(endpoint, headers=_HEADERS, timeout=_FETCH_TIMEOUT)
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    return ResolvedMetadata(
        title=data.get("title") or None,
        description=data.get("author_name"),
        image_url=data.get("thumbnail_url"),
        via="oembed",
    )


async def _tumblr_oembed(url: str, client: httpx.AsyncClient) -> ResolvedMetadata | None:
    endpoint = f"https://www.tumblr.com/oembed/1.0?url={quote(url, safe='')}"
    resp = await client.get(endpoint, headers=_HEADERS, timeout=_FETCH_TIMEOUT)
    if resp.status_code != 200:
        return None
    data = resp.json()
    # Photo posts: prefer full image URL; all types fall back to thumbnail.
    image = data.get("thumbnail_url")
    if data.get("type") == "photo":
        image = data.get("url") or image
    return ResolvedMetadata(
        title=data.get("title") or None,
        description=data.get("author_name"),
        image_url=image,
        via="oembed",
    )


def _fxtwitter_media(tweet: dict) -> tuple[str | None, dict | None]:
    """Return (still_image_url, video_dict) from an fxtwitter tweet dict.

    Photos: direct URL, no video. Videos and GIFs: the thumbnail still plus the
    media dict itself - fxtwitter serves both types as direct video/mp4 URLs
    (with width/height), which Bluesky can host natively.
    """
    media = tweet.get("media") or {}
    photos = media.get("photos") or []
    if photos:
        return photos[0].get("url"), None
    videos = media.get("videos") or []
    if videos:
        return videos[0].get("thumbnail_url"), videos[0]
    return None, None


async def _twitter_fxtwitter_api(url: str, client: httpx.AsyncClient) -> ResolvedMetadata | None:
    """Resolve Twitter/X metadata via the fxtwitter JSON API, with vxtwitter fallback.

    The fxtwitter OG page omits og:image for multi-image tweets, so the bot
    would fall back to the Discord-embed mosaic URL which 403s. The JSON API
    returns proper pbs.twimg.com photo URLs that download cleanly.
    vxtwitter is tried as a fallback because some tweets that 404 on fxtwitter
    resolve correctly there (different backend coverage).
    """
    from urllib.parse import urlparse
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 3 or parts[1] != "status":
        return None
    handle, tweet_id = parts[0], parts[2]

    # A tweet that needs an authenticated, age-verified session (protected account or,
    # far more commonly, age-restricted media) can't be fetched by the guest-token
    # mirrors. fxtwitter answers 401 PRIVATE_TWEET for it; vxtwitter's scan then fails
    # too. When both give up that way we report it as "unavailable" so the caller can
    # tell the curator to attach the media directly rather than showing a dead card.
    needs_auth = False

    endpoint = f"https://api.fxtwitter.com/{handle}/status/{tweet_id}"
    try:
        resp = await client.get(endpoint, headers=_HEADERS, timeout=_FETCH_TIMEOUT)
        if resp.status_code == 200:
            tweet = resp.json().get("tweet", {})
            if tweet:
                image_url, video = _fxtwitter_media(tweet)
                if not image_url and not video:
                    image_url, video = _fxtwitter_media(tweet.get("quote") or {})
                return ResolvedMetadata(
                    title=tweet.get("text"),
                    description=tweet.get("author", {}).get("name"),
                    image_url=image_url,
                    video_url=(video or {}).get("url"),
                    video_width=(video or {}).get("width"),
                    video_height=(video or {}).get("height"),
                    via="fxtwitter_api",
                )
        else:
            log.info("fxtwitter api returned %d for %s", resp.status_code, url)
            if resp.status_code in (401, 403):
                needs_auth = True
    except httpx.HTTPError:
        pass

    _blocked = ResolvedMetadata(via="unavailable") if needs_auth else None

    # Fallback: vxtwitter API (different response format, different backend)
    endpoint = f"https://api.vxtwitter.com/{handle}/status/{tweet_id}"
    try:
        resp = await client.get(endpoint, headers=_HEADERS, timeout=_FETCH_TIMEOUT)
    except httpx.HTTPError:
        return _blocked
    if resp.status_code != 200:
        log.info("vxtwitter api returned %d for %s", resp.status_code, url)
        return _blocked
    try:
        data = resp.json()
    except ValueError:
        log.info("vxtwitter api returned non-JSON for %s", url)
        return _blocked
    # media_extended distinguishes images from videos/GIFs; the flat mediaURLs
    # list would hand an .mp4 URL to the thumbnail downloader for video tweets.
    image_url = None
    video: dict | None = None
    for m in data.get("media_extended") or []:
        if m.get("type") in ("video", "gif") and video is None:
            video = m
        elif m.get("type") == "image" and image_url is None:
            image_url = m.get("url")
    if image_url is None and video is not None:
        image_url = video.get("thumbnail_url")
    if image_url is None and video is None:
        media_urls = data.get("mediaURLs") or []
        image_url = media_urls[0] if media_urls else None
    video_size = (video or {}).get("size") or {}
    return ResolvedMetadata(
        title=data.get("text"),
        description=data.get("user_name"),
        image_url=image_url,
        video_url=(video or {}).get("url"),
        video_width=video_size.get("width"),
        video_height=video_size.get("height"),
        via="vxtwitter_api",
    )


async def _tiktok_tikwm(url: str, client: httpx.AsyncClient) -> ResolvedMetadata | None:
    """Fetch TikTok cover thumbnail via tikwm.com.

    TikTok serves JS-only pages to bots and its own oEmbed gives only the author
    name with no caption. tikwm.com returns a stable cover image URL. We return
    title=None intentionally so that resolve() falls back to the Discord-captured
    embed_title (which Discord may have fetched via its own TikTok integration).
    """
    endpoint = f"https://tikwm.com/api/?url={quote(url, safe='')}"
    try:
        resp = await client.get(endpoint, headers=_HEADERS, timeout=_FETCH_TIMEOUT)
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json().get("data") or {}
    except ValueError:
        return None
    cover = data.get("cover") or data.get("origin_cover")
    author = (data.get("author") or {}).get("nickname")
    if not cover and not author:
        return None
    return ResolvedMetadata(
        title=None,
        description=author,
        image_url=cover or None,
        video_url=data.get("play") or None,  # watermark-free direct mp4
        via="tikwm",
    )


async def _wikipedia_api(url: str, client: httpx.AsyncClient) -> ResolvedMetadata | None:
    """Resolve metadata via the Wikipedia REST API summary endpoint.

    Preferred over raw HTML scraping: the REST API is documented for programmatic
    use, more lenient on datacenter IPs, and returns structured thumbnail data
    without HTML parsing.
    """
    from urllib.parse import urlparse
    parsed = urlparse(url)
    parts = parsed.path.split("/wiki/", 1)
    if len(parts) < 2 or not parts[1]:
        return None
    title = parts[1].rstrip("/")
    api_url = f"https://{parsed.netloc}/api/rest_v1/page/summary/{title}"
    headers = {**_HEADERS, "Accept": "application/json"}
    try:
        resp = await client.get(api_url, headers=headers, timeout=_FETCH_TIMEOUT)
    except httpx.HTTPError as exc:
        log.info("wikipedia api request failed for %s: %s", url, exc)
        return None
    if resp.status_code != 200:
        log.info("wikipedia api returned %d for %s", resp.status_code, url)
        return None
    try:
        data = resp.json()
    except ValueError as exc:
        log.info("wikipedia api parse error for %s: %s", url, exc)
        return None
    image_url = (data.get("thumbnail") or data.get("originalimage") or {}).get("source")
    return ResolvedMetadata(
        title=data.get("title"),
        description=data.get("description"),
        image_url=image_url,
        via="wikipedia_api",
    )


def _reddit_video(post: dict) -> dict | None:
    """Return the reddit_video dict for a v.redd.it post, or None."""
    rv = ((post.get("secure_media") or post.get("media") or {}) or {}).get("reddit_video") or {}
    return rv if rv.get("fallback_url") else None


def _reddit_video_fields(rv: dict) -> tuple[str | None, bool]:
    """Map a reddit_video dict to (video_url, is_stream).

    GIFs have no audio, so their video-only fallback_url can be downloaded directly.
    Regular videos store audio as a separate stream, so we hand the HLS/DASH manifest
    to ffmpeg to fetch and mux; without a manifest we skip (a bare fallback_url would
    be silent).
    """
    if rv.get("is_gif"):
        return rv.get("fallback_url"), False
    manifest = rv.get("hls_url") or rv.get("dash_url")
    if manifest:
        return manifest, True
    return None, False


def _reddit_image_url(post: dict) -> str | None:
    """Extract the best available image URL from a Reddit post data dict."""
    url = post.get("url", "")
    if url and any(url.startswith(p) for p in ("https://i.redd.it/", "https://preview.redd.it/")):
        return url
    if url and any(url.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp")):
        return url
    # Preview images exist for most post types (link posts, crossposts, etc.)
    # Reddit HTML-encodes & as &amp; inside the JSON strings.
    try:
        source = post["preview"]["images"][0]["source"]
        img = source.get("url", "").replace("&amp;", "&")
        if img:
            return img
    except (KeyError, IndexError):
        pass
    return None


async def _reddit_json_api(url: str, client: httpx.AsyncClient) -> ResolvedMetadata | None:
    """Fetch Reddit post metadata via the JSON API.

    Handles crossposts (Reddit reposts) by falling through to
    crosspost_parent_list[0] when the outer post has no direct image.
    The vxreddit OpenGraph mirror only sees the crosspost wrapper and
    returns a low-res thumbnail; the JSON API returns the original image.
    """
    from urllib.parse import urlparse
    parsed = urlparse(url)
    # Build .json endpoint; limit=1 skips loading all comments.
    json_url = f"https://www.reddit.com{parsed.path.rstrip('/')}.json?limit=1"
    headers = {**_HEADERS, "Accept": "application/json"}
    try:
        resp = await client.get(json_url, headers=headers, timeout=_FETCH_TIMEOUT)
    except httpx.HTTPError as exc:
        log.info("reddit json api request failed for %s: %s", url, exc)
        return None
    if resp.status_code != 200:
        log.info("reddit json api returned %d for %s", resp.status_code, url)
        return None
    try:
        data = resp.json()
        post = data[0]["data"]["children"][0]["data"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        log.info("reddit json api unexpected shape for %s: %s", url, exc)
        return None

    title = post.get("title")
    subreddit = post.get("subreddit_name_prefixed")

    # Try to get an image from the post itself first.
    image_url = _reddit_image_url(post)
    rv = _reddit_video(post)

    # For crossposts, fall through to the original post's media.
    if post.get("crosspost_parent_list"):
        parent = post["crosspost_parent_list"][0]
        if not image_url:
            image_url = _reddit_image_url(parent)
        if not rv:
            rv = _reddit_video(parent)

    video_url, video_is_stream = _reddit_video_fields(rv) if rv else (None, False)
    return ResolvedMetadata(
        title=title,
        description=subreddit,
        image_url=image_url,
        video_url=video_url,
        video_width=(rv or {}).get("width"),
        video_height=(rv or {}).get("height"),
        video_is_stream=video_is_stream,
        via="reddit_api",
    )


def _extract_og_video(html: str) -> str | None:
    """Pull a direct video URL from a page's OpenGraph tags (og:video:secure_url)."""
    parser = _MetaParser()
    parser.feed(html)
    url = _first(parser.metas, "og:video:secure_url", "og:video:url", "og:video")
    return url.replace("&amp;", "&") if url else None


async def _reddit_mirror(url: str, client: httpx.AsyncClient) -> ResolvedMetadata | None:
    """Resolve reddit via the vxreddit embed mirror.

    Reddit's own JSON API 403s from datacenter IPs. The mirror serves an OpenGraph
    page (only to a recognised crawler UA - see _CRAWLER_HEADERS; a plain UA is bounced
    to reddit) whose og:video is a server-side muxed mp4 - video + the separate audio
    track combined - that downloads directly. Used when the JSON API yields no video.
    """
    mirror_url = _reddit_mirror_url(url)
    if mirror_url == url:
        return None
    try:
        resp = await client.get(mirror_url, headers=_CRAWLER_HEADERS, timeout=_FETCH_TIMEOUT, follow_redirects=True)
    except httpx.HTTPError as exc:
        log.info("reddit mirror fetch failed for %s: %s", url, exc)
        return None
    if resp.status_code != 200 or "html" not in resp.headers.get("content-type", ""):
        log.info("reddit mirror returned %d (%s) for %s", resp.status_code, resp.headers.get("content-type", ""), url)
        return None
    html = resp.text[:_MAX_HTML_BYTES]
    meta = parse_html_metadata(html, str(resp.url))
    video = _extract_og_video(html)
    if video:
        meta.video_url = video
        meta.video_is_stream = False  # mirror mp4 is pre-muxed and directly downloadable
    meta.via = "reddit_mirror"
    return meta


async def _reddit_resolve(url: str, client: httpx.AsyncClient) -> ResolvedMetadata | None:
    """Reddit resolution, tiered for resilience.

    1. reddit.com JSON API - richest (full reddit_video manifest); works off
       datacenter IPs or if reddit unblocks this host. A success is trusted whole.
    2. vxreddit mirror - only when the JSON API fails (e.g. 403 on a datacenter IP);
       serves a muxed mp4 in og:video that downloads directly.
    """
    meta = await _reddit_json_api(url, client)
    if meta is not None:
        return meta
    return await _reddit_mirror(url, client)


def _twitter_mirror_url(url: str) -> str:
    return url.replace("https://twitter.com/", "https://fxtwitter.com/", 1)


def _reddit_mirror_url(url: str) -> str:
    return url.replace("https://www.reddit.com/", "https://vxreddit.com/", 1)


def _instagram_mirror_url(url: str) -> str:
    return url.replace("https://www.instagram.com/", "https://kkinstagram.com/", 1)


def _deviantart_mirror_url(url: str) -> str:
    return url.replace("https://www.deviantart.com", "https://fixdeviantart.com", 1)


def _youtube_watch_url(url: str) -> str:
    """Convert youtu.be/ID short URLs to youtube.com/watch?v=ID.

    YouTube's watch pages serve proper OG tags; the short URL form often returns
    a minimal redirect page with no OG metadata when fetched by a bot UA.
    """
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.netloc == "youtu.be":
        video_id = parsed.path.lstrip("/")
        if video_id:
            return f"https://www.youtube.com/watch?v={video_id}"
    return url


_OEMBED_HANDLERS = {
    "youtube": _youtube_oembed,
    "deviantart": _deviantart_oembed,
    "tumblr": _tumblr_oembed,
    "twitter": _twitter_fxtwitter_api,
    "instagram": _instagram_oembed,
    "tiktok": _tiktok_tikwm,
    "reddit": _reddit_resolve,
    "wikipedia": _wikipedia_api,
}

# Note: reddit is absent here - _reddit_resolve already consults the vxreddit mirror
# internally (and extracts og:video), so a generic mirror-OG retry would be redundant.
_MIRROR_URL_FUNCS = {
    "twitter": _twitter_mirror_url,
    "instagram": _instagram_mirror_url,
    "deviantart": _deviantart_mirror_url,
    "youtube": _youtube_watch_url,
}


async def _fetch_opengraph(url: str, client: httpx.AsyncClient) -> ResolvedMetadata | None:
    resp = await client.get(
        url, headers=_HEADERS, timeout=_FETCH_TIMEOUT, follow_redirects=True
    )
    resp.raise_for_status()
    if "html" not in resp.headers.get("content-type", ""):
        return None
    html = resp.text[:_MAX_HTML_BYTES]
    return parse_html_metadata(html, str(resp.url))


async def resolve(
    url: str,
    family: str,
    *,
    client: httpx.AsyncClient,
    fallback_title: str | None = None,
    fallback_description: str | None = None,
    fallback_image_url: str | None = None,
    youtube_api_key: str | None = None,
) -> ResolvedMetadata:
    """Resolve metadata for one canonical link, with Discord-embed fallback.

    Never raises: network/parse failures degrade to the fallback, then to none.
    """
    if family == "bluesky":
        # Native repost/quote: metadata comes from the record itself at publish.
        return ResolvedMetadata(via="skipped")

    meta: ResolvedMetadata | None = None
    # 0. YouTube Data API v3 - preferred over scraping; works from datacenter IPs.
    if family == "youtube" and youtube_api_key:
        try:
            meta = await _youtube_api(url, client, youtube_api_key)
        except (httpx.HTTPError, ValueError, UnicodeDecodeError) as exc:
            log.info("youtube api fetch failed for %s: %s", url, exc)
    # 1. Try oEmbed if available for this family.
    if meta is None and family in _OEMBED_HANDLERS:
        try:
            meta = await _OEMBED_HANDLERS[family](url, client)
        except (httpx.HTTPError, ValueError, UnicodeDecodeError) as exc:
            log.info("oembed fetch failed for %s: %s", url, exc)
    # A resolver that reports the source is fetch-blocked (age-restricted / private)
    # short-circuits: the mirror/canonical OG pages for those return a junk "post
    # doesn't exist" card, so fall through only to a Discord-captured embed if we have
    # one, otherwise propagate "unavailable" for the caller to explain.
    if meta is not None and meta.via == "unavailable":
        if fallback_title or fallback_image_url:
            return ResolvedMetadata(
                title=fallback_title,
                description=fallback_description,
                image_url=fallback_image_url,
                via="discord",
            )
        return ResolvedMetadata(via="unavailable")
    # 2. Try mirror URL for OpenGraph (better than canonical for blocked platforms).
    if meta is None and family in _MIRROR_URL_FUNCS:
        mirror = _MIRROR_URL_FUNCS[family](url)
        if mirror != url:
            try:
                meta = await _fetch_opengraph(mirror, client)
            except (httpx.HTTPError, ValueError, UnicodeDecodeError) as exc:
                log.info("mirror opengraph fetch failed for %s: %s", mirror, exc)
    # 3. Fall back to fetching canonical URL directly.
    if meta is None:
        try:
            meta = await _fetch_opengraph(url, client)
        except (httpx.HTTPError, ValueError, UnicodeDecodeError) as exc:
            log.info("opengraph fetch failed for %s: %s", url, exc)

    meta = meta or ResolvedMetadata(via="none")
    fetched_anything = bool(meta.title or meta.image_url or meta.video_url)

    title = meta.title or fallback_title
    description = meta.description or fallback_description
    image_url = meta.image_url or fallback_image_url

    if fetched_anything:
        via = meta.via
    elif fallback_title or fallback_image_url:
        via = "discord"
    else:
        via = "none"
    return ResolvedMetadata(
        title=title, description=description, image_url=image_url, via=via,
        video_url=meta.video_url,
        video_width=meta.video_width,
        video_height=meta.video_height,
        video_is_stream=meta.video_is_stream,
    )
