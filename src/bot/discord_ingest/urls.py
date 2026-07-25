"""Extract URLs from raw Discord message content."""

from __future__ import annotations

import re
from urllib.parse import urlparse

# Matches http(s) URLs, stopping at whitespace and angle brackets. Discord wraps
# suppressed-embed links in <...>, which we strip.
_URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)

# Discord navigation links (jump-to-message, channel/thread links, invites) are
# never source content - they turn up when someone quotes or forwards a Discord
# message. CDN/media hosts are deliberately NOT in this set: a pasted
# cdn.discordapp.com/media.discordapp.net URL can be a legitimate attachment.
_DISCORD_NAV_HOSTS = ("discord.com", "discordapp.com", "discord.gg")
_DISCORD_MEDIA_HOSTS = ("cdn.discordapp.com", "media.discordapp.net", "cdn.discord.com")


def is_discord_internal_url(url: str) -> bool:
    """True for a Discord jump/message/channel link or invite (never a source),
    False for CDN/media hosts and everything else."""
    host = (urlparse(url).hostname or "").lower()
    if host in _DISCORD_MEDIA_HOSTS:
        return False
    return any(host == h or host.endswith("." + h) for h in _DISCORD_NAV_HOSTS)
# Trailing punctuation that is almost never part of the URL itself (excluding ")").
# Includes Discord markdown wrappers - `|` (spoiler ||...||), `*` (bold/italic), and
# backtick (inline code) - which cling to the end of a pasted link. `_` and `~` are
# deliberately excluded: they can legitimately end a URL path.
_TRAILING = ".,;!?\"'>]}|*`"


def _strip_trailing(url: str) -> str:
    """Strip trailing punctuation, preserving balanced parentheses.

    Plain rstrip(")") breaks Wikipedia URLs like /wiki/Stanley_(vehicle) where
    the closing paren is part of the path. Only strip trailing ) when unbalanced.
    """
    url = url.rstrip(_TRAILING)
    while url.endswith(")") and url.count("(") < url.count(")"):
        url = url[:-1]
    return url


def extract_urls(content: str) -> list[str]:
    """Return URLs in first-seen order, de-duplicated, with trailing punctuation trimmed."""
    found: list[str] = []
    seen: set[str] = set()
    for match in _URL_RE.finditer(content or ""):
        url = _strip_trailing(match.group(0))
        if url not in seen:
            seen.add(url)
            found.append(url)
    return found
