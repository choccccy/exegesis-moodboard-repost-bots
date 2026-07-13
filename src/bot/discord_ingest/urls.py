"""Extract URLs from raw Discord message content."""

from __future__ import annotations

import re

# Matches http(s) URLs, stopping at whitespace and angle brackets. Discord wraps
# suppressed-embed links in <...>, which we strip.
_URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
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
