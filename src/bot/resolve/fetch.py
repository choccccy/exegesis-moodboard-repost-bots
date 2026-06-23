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


async def _youtube_oembed(url: str, client: httpx.AsyncClient) -> ResolvedMetadata | None:
    endpoint = f"https://www.youtube.com/oembed?format=json&url={quote(url, safe='')}"
    resp = await client.get(endpoint, headers=_HEADERS, timeout=_FETCH_TIMEOUT)
    if resp.status_code != 200:
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


def _twitter_mirror_url(url: str) -> str:
    return url.replace("https://twitter.com/", "https://fxtwitter.com/", 1)


def _reddit_mirror_url(url: str) -> str:
    return url.replace("https://www.reddit.com/", "https://vxreddit.com/", 1)


def _instagram_mirror_url(url: str) -> str:
    return url.replace("https://www.instagram.com/", "https://kkinstagram.com/", 1)


def _deviantart_mirror_url(url: str) -> str:
    return url.replace("https://www.deviantart.com", "https://fixdeviantart.com", 1)


_OEMBED_HANDLERS = {
    "youtube": _youtube_oembed,
    "deviantart": _deviantart_oembed,
}

_MIRROR_URL_FUNCS = {
    "twitter": _twitter_mirror_url,
    "reddit": _reddit_mirror_url,
    "instagram": _instagram_mirror_url,
    "deviantart": _deviantart_mirror_url,
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
) -> ResolvedMetadata:
    """Resolve metadata for one canonical link, with Discord-embed fallback.

    Never raises: network/parse failures degrade to the fallback, then to none.
    """
    if family == "bluesky":
        # Native repost/quote: metadata comes from the record itself at publish.
        return ResolvedMetadata(via="skipped")

    meta: ResolvedMetadata | None = None
    try:
        # 1. Try oEmbed if available for this family.
        if family in _OEMBED_HANDLERS:
            meta = await _OEMBED_HANDLERS[family](url, client)
        # 2. Try mirror URL for OpenGraph (better than canonical for blocked platforms).
        if meta is None and family in _MIRROR_URL_FUNCS:
            mirror = _MIRROR_URL_FUNCS[family](url)
            if mirror != url:
                meta = await _fetch_opengraph(mirror, client)
        # 3. Fall back to fetching canonical URL directly.
        if meta is None:
            meta = await _fetch_opengraph(url, client)
    except (httpx.HTTPError, ValueError, UnicodeDecodeError) as exc:
        log.info("metadata resolve failed for %s: %s", url, exc)
        meta = None

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
