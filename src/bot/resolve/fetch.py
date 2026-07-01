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
_FETCH_TIMEOUT = 15.0
_MAX_HTML_BYTES = 2_000_000  # don't parse arbitrarily large bodies


@dataclass
class ResolvedMetadata:
    title: str | None = None
    description: str | None = None
    image_url: str | None = None
    via: str = "none"  # oembed | opengraph | html | discord | none | skipped


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

    endpoint = f"https://api.fxtwitter.com/{handle}/status/{tweet_id}"
    try:
        resp = await client.get(endpoint, headers=_HEADERS, timeout=_FETCH_TIMEOUT)
        if resp.status_code == 200:
            tweet = resp.json().get("tweet", {})
            if tweet:
                photos = tweet.get("media", {}).get("photos", [])
                if not photos:
                    quoted = tweet.get("quote") or {}
                    photos = quoted.get("media", {}).get("photos", [])
                image_url = photos[0]["url"] if photos else None
                return ResolvedMetadata(
                    title=tweet.get("text"),
                    description=tweet.get("author", {}).get("name"),
                    image_url=image_url,
                    via="fxtwitter_api",
                )
        else:
            log.info("fxtwitter api returned %d for %s", resp.status_code, url)
    except httpx.HTTPError:
        pass

    # Fallback: vxtwitter API (different response format, different backend)
    endpoint = f"https://api.vxtwitter.com/{handle}/status/{tweet_id}"
    try:
        resp = await client.get(endpoint, headers=_HEADERS, timeout=_FETCH_TIMEOUT)
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        log.info("vxtwitter api returned %d for %s", resp.status_code, url)
        return None
    try:
        data = resp.json()
    except ValueError:
        log.info("vxtwitter api returned non-JSON for %s", url)
        return None
    media_urls = data.get("mediaURLs") or []
    return ResolvedMetadata(
        title=data.get("text"),
        description=data.get("user_name"),
        image_url=media_urls[0] if media_urls else None,
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
        via="tikwm",
    )


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
}

_MIRROR_URL_FUNCS = {
    "twitter": _twitter_mirror_url,
    "reddit": _reddit_mirror_url,
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
    fetched_anything = bool(meta.title or meta.image_url)

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
        title=title, description=description, image_url=image_url, via=via
    )
