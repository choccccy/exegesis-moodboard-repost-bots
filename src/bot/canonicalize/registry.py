"""Per-domain canonicalization rules + a default tracker-stripping fallback.

Add a new domain by writing a handler that takes the parsed URL parts and returns
a (canonical_url, domain_family) tuple, then registering it in ``_HANDLERS``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query params that never change *what* a resource is - safe to strip everywhere.
TRACKING_PARAMS: frozenset[str] = frozenset(
    {
        "si", "fbclid", "gclid", "dclid", "yclid", "msclkid",
        "igshid", "igsh", "ref_src", "ref_url", "ref", "source",
        "mc_cid", "mc_eid", "spm", "scwid", "share_id",
        "feature", "ab_channel",  # youtube share noise
        "s", "t",  # twitter/x share params (YouTube handled specially)
    }
)
# utm_* are always tracking.
_UTM_RE = re.compile(r"^utm_", re.IGNORECASE)


@dataclass(frozen=True)
class CanonResult:
    canonical_url: str
    domain_family: str


def _strip_tracking(query: str, *, keep: frozenset[str] = frozenset()) -> str:
    pairs = parse_qsl(query, keep_blank_values=True)
    cleaned = [
        (k, v)
        for k, v in pairs
        if k in keep or (not _UTM_RE.match(k) and k not in TRACKING_PARAMS)
    ]
    return urlencode(cleaned)


def _host(netloc: str) -> str:
    return netloc.split("@")[-1].split(":")[0].lower()


def _family_from_host(host: str) -> str | None:
    h = host[4:] if host.startswith("www.") else host
    parts = h.split(".")
    for fam, domains in _DOMAIN_FAMILIES.items():
        if any(h == d or h.endswith("." + d) for d in domains):
            return fam
    # crude second-level guess for the unknown-domain default
    return None


# host substrings -> family
_DOMAIN_FAMILIES: dict[str, list[str]] = {
    "bluesky": ["bsky.app", "bsky.social", "xbsky.app"],
    "reddit": ["reddit.com", "redd.it", "vxreddit.com", "rxddit.com"],
    "twitter": [
        "twitter.com", "x.com",
        "fxtwitter.com", "fixupx.com", "vxtwitter.com", "fixvx.com",
        "nitter.net", "girlcockx.com", "xcancel.com",
    ],
    "artstation": ["artstation.com", "artstn.co"],
    "deviantart": ["deviantart.com", "fav.me", "fixdeviantart.com"],
    "wikipedia": ["wikipedia.org"],  # mobile (en.m.wikipedia.org) matched via suffix
    "instagram": ["instagram.com", "ddinstagram.com", "instagramez.com", "kkinstagram.com"],
    "youtube": ["youtube.com", "youtu.be", "youtube-nocookie.com"],
    "pixiv":   ["pixiv.net"],
    "flickr":  ["flickr.com", "flic.kr"],
    "tumblr":  ["tumblr.com"],
    "tiktok":  ["tiktok.com", "vm.tiktok.com", "m.tiktok.com", "kktiktok.com"],
}


# --- per-domain handlers ----------------------------------------------------


def _canon_bluesky(scheme, host, path, query) -> tuple[str, str]:
    # bsky.app/profile/<handle-or-did>/post/<rkey> - drop all query.
    return urlunsplit(("https", "bsky.app", path.rstrip("/"), "", "")), "bluesky"


def _canon_reddit(scheme, host, path, query) -> tuple[str, str]:
    # Collapse old./new./np./m. and amp variants to canonical www.reddit.com.
    return urlunsplit(("https", "www.reddit.com", path.rstrip("/"), "", "")), "reddit"


def _canon_twitter(scheme, host, path, query) -> tuple[str, str]:
    # Normalize to twitter.com (project preference); mirrors are fetch-only.
    return urlunsplit(("https", "twitter.com", path.rstrip("/"), "", "")), "twitter"


def _canon_artstation(scheme, host, path, query) -> tuple[str, str]:
    return urlunsplit(("https", "www.artstation.com", path.rstrip("/"), "", "")), "artstation"


def _canon_deviantart(scheme, host, path, query) -> tuple[str, str]:
    # Normalize fix mirror to canonical.
    if "fixdeviantart.com" in host:
        return urlunsplit(("https", "www.deviantart.com", path.rstrip("/"), "", "")), "deviantart"
    # Old-style artist subdomains (artist.deviantart.com) -> www.deviantart.com/artist/...
    if host.endswith(".deviantart.com") and not host.startswith(("www.", "backend.")):
        username = host.split(".deviantart.com")[0]
        new_path = f"/{username}{path.rstrip('/')}"
        return urlunsplit(("https", "www.deviantart.com", new_path, "", "")), "deviantart"
    # fav.me short links and www.deviantart.com: strip query.
    return urlunsplit(("https", "www.deviantart.com", path.rstrip("/"), "", "")), "deviantart"


def _canon_wikipedia(scheme, host, path, query) -> tuple[str, str]:
    # Convert mobile (xx.m.wikipedia.org) to desktop canonical (xx.wikipedia.org).
    desktop = host.replace(".m.wikipedia.org", ".wikipedia.org")
    return urlunsplit(("https", desktop, path, "", "")), "wikipedia"


def _canon_instagram(scheme, host, path, query) -> tuple[str, str]:
    # Normalize mirror hosts to instagram.com and ensure trailing slash on /p/.
    segments = [s for s in path.split("/") if s]
    if len(segments) >= 2 and segments[0] in {"p", "reel", "tv"}:
        path = f"/{segments[0]}/{segments[1]}/"
    return urlunsplit(("https", "www.instagram.com", path, "", "")), "instagram"


_YT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def _canon_youtube(scheme, host, path, query) -> tuple[str, str]:
    params = dict(parse_qsl(query, keep_blank_values=True))
    video_id: str | None = None
    if host.endswith("youtu.be"):
        video_id = path.lstrip("/").split("/")[0] or None
    elif path == "/watch":
        video_id = params.get("v")
    elif path.startswith(("/shorts/", "/embed/", "/live/")):
        video_id = path.split("/")[2] if len(path.split("/")) > 2 else None

    # Preserve an intentional timestamp; drop playlists/trackers.
    ts = params.get("t") or params.get("start")
    if video_id and _YT_ID_RE.match(video_id):
        q = urlencode({"t": ts}) if ts else ""
        return urlunsplit(("https", "youtu.be", f"/{video_id}", q, "")), "youtube"
    # Not a recognizable video URL (channel/playlist page): keep host, strip trackers.
    keep = frozenset({"t", "start"})
    return urlunsplit(("https", host, path, _strip_tracking(query, keep=keep), "")), "youtube"


def _canon_pixiv(scheme, host, path, query) -> tuple[str, str]:
    return urlunsplit(("https", "www.pixiv.net", path.rstrip("/"), "", "")), "pixiv"


def _canon_flickr(scheme, host, path, query) -> tuple[str, str]:
    # flic.kr short links redirect; keep path as-is (follow_redirects handles it).
    return urlunsplit(("https", "www.flickr.com", path, "", "")), "flickr"


_TUMBLR_POST_RE = re.compile(r"^(/post/\d+)(?:/[^/]*)?$")


def _canon_tumblr(scheme, host, path, query) -> tuple[str, str]:
    # Preserve username.tumblr.com subdomain (it's the blog identity).
    # Strip optional title slug: /post/123456789/some-slug → /post/123456789
    # Drop all query params (source=share, etc.).
    m = _TUMBLR_POST_RE.match(path)
    canonical_path = m.group(1) if m else path.rstrip("/")
    return urlunsplit(("https", host, canonical_path, "", "")), "tumblr"


def _canon_tiktok(scheme, host, path, query) -> tuple[str, str]:
    # Normalize to www.tiktok.com; keep only /@user/video/ID path, strip tracking.
    segments = [s for s in path.split("/") if s]
    if (
        len(segments) >= 3
        and segments[0].startswith("@")
        and segments[1] == "video"
        and segments[2].isdigit()
    ):
        clean_path = f"/{segments[0]}/video/{segments[2]}"
        return urlunsplit(("https", "www.tiktok.com", clean_path, "", "")), "tiktok"
    # vm.tiktok.com / unknown path forms: keep as-is, strip query.
    return urlunsplit(("https", host, path, "", "")), "tiktok"


_HANDLERS = {
    "bluesky": _canon_bluesky,
    "reddit": _canon_reddit,
    "twitter": _canon_twitter,
    "artstation": _canon_artstation,
    "deviantart": _canon_deviantart,
    "wikipedia": _canon_wikipedia,
    "instagram": _canon_instagram,
    "youtube": _canon_youtube,
    "pixiv": _canon_pixiv,
    "flickr": _canon_flickr,
    "tumblr": _canon_tumblr,
    "tiktok": _canon_tiktok,
}


# Path-pattern heuristics for unknown mirrors that follow well-known URL conventions.
# Fires only when host lookup fails (family is None).
_PATH_HEURISTICS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^/[^/]+/status/\d+(/|$)"), "twitter"),              # /handle/status/ID
    (re.compile(r"^/r/[^/]+/comments/[^/]+"), "reddit"),               # /r/sub/comments/rkey
    (re.compile(r"^/profile/[^/]+/post/[^/]+"), "bluesky"),            # /profile/handle/post/rkey
    (re.compile(r"^/(?:p|reel|tv)/[A-Za-z0-9_-]+"), "instagram"),     # /p/CODE/ or /reel/CODE/
    (re.compile(r"^/wiki/[^/]+"), "wikipedia"),                         # /wiki/PageName
    (re.compile(r"^/artwork/[^/]+"), "artstation"),                     # /artwork/slug
    (re.compile(r"^/shorts/[A-Za-z0-9_-]{11}(/|$)"), "youtube"),      # /shorts/11-char-ID
    (re.compile(r"^/artworks/\d+"), "pixiv"),                           # /artworks/12345678
    (re.compile(r"^/photos/[^/]+/\d+"), "flickr"),                     # /photos/user/ID
    (re.compile(r"^/@[^/]+/video/\d+"), "tiktok"),                     # /@user/video/ID
]


def _infer_family_from_path(path: str) -> str | None:
    for pattern, family in _PATH_HEURISTICS:
        if pattern.search(path):
            return family
    return None


def canonicalize(url: str) -> CanonResult:
    """Return the canonical form of ``url`` plus its domain family.

    Unknown domains keep their structure but have tracking params stripped.
    """
    url = url.strip()
    parts = urlsplit(url if "://" in url else f"https://{url}")
    host = _host(parts.netloc)
    family = _family_from_host(host)

    if family is None:
        family = _infer_family_from_path(parts.path)

    if family and family in _HANDLERS:
        canonical, fam = _HANDLERS[family](parts.scheme, host, parts.path, parts.query)
        return CanonResult(canonical_url=canonical, domain_family=fam)

    # Default: force https, strip trackers, keep everything else.
    cleaned_query = _strip_tracking(parts.query)
    canonical = urlunsplit(("https", parts.netloc.lower(), parts.path, cleaned_query, ""))
    return CanonResult(canonical_url=canonical, domain_family="other")
