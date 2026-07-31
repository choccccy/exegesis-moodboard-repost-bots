"""Known-good embed mirrors - the human-facing cheat-sheet + nag source of truth.

Several platforms embed poorly on Bluesky/Discord unless you paste a specific
community mirror (e.g. ``tnktok.com`` for TikTok). The bot already *resolves* via
these mirrors internally (see ``resolve/fetch.py``), but people still have to
remember which one to paste to get a good native preview. This module is the one
curated list, consumed by the dashboard cheat-sheet and the in-thread nag.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from .canonicalize import canonicalize


@dataclass(frozen=True)
class Mirror:
    family: str  # canonicalize() domain_family this mirror serves
    label: str  # human platform name
    host: str  # the good mirror host to paste
    example: str  # a sample rewritten URL


# Ordered for display. Values align with resolve/fetch.py's _MIRROR_URL_FUNCS.
# Bluesky is intentionally absent - it's a native repost, no mirror needed.
KNOWN_GOOD_MIRRORS: tuple[Mirror, ...] = (
    Mirror("tiktok", "TikTok", "tnktok.com", "https://tnktok.com/@user/video/123456789"),
    Mirror("twitter", "X / Twitter", "fxtwitter.com", "https://fxtwitter.com/user/status/123456789"),
    Mirror("reddit", "Reddit", "vxreddit.com", "https://vxreddit.com/r/sub/comments/abc123/title"),
    Mirror("instagram", "Instagram", "kkinstagram.com", "https://kkinstagram.com/p/CshORoLs/"),
    Mirror("deviantart", "DeviantArt", "fixdeviantart.com", "https://fixdeviantart.com/user/art/title-123456789"),
)

_BY_FAMILY: dict[str, Mirror] = {m.family: m for m in KNOWN_GOOD_MIRRORS}


def _bare_host(url: str) -> str:
    host = urlsplit(url).hostname or ""
    return host.lower().removeprefix("www.")


def mirror_hint_for_url(url: str) -> str | None:
    """A 'paste the <mirror> mirror' tip for a URL whose platform has a known-good
    mirror and that isn't already using it; else ``None``.

    Pass the *raw* submitted URL - canonicalization strips mirrors back to the
    canonical host, which would hide whether the good mirror was already used.
    """
    mirror = _BY_FAMILY.get(canonicalize(url).domain_family)
    if mirror is None or _bare_host(url) == mirror.host:
        return None
    return f"tip: {mirror.label} embeds better via the `{mirror.host}` mirror - reply with that link"
