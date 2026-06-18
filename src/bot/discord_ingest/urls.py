"""Extract URLs from raw Discord message content."""

from __future__ import annotations

import re

# Matches http(s) URLs, stopping at whitespace and angle brackets. Discord wraps
# suppressed-embed links in <...>, which we strip.
_URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
# Trailing punctuation that is almost never part of the URL itself.
_TRAILING = ").,;!?\"'>]}"


def extract_urls(content: str) -> list[str]:
    """Return URLs in first-seen order, de-duplicated, with trailing punctuation trimmed."""
    found: list[str] = []
    seen: set[str] = set()
    for match in _URL_RE.finditer(content or ""):
        url = match.group(0).rstrip(_TRAILING)
        if url not in seen:
            seen.add(url)
            found.append(url)
    return found
