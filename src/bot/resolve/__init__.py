"""Source metadata resolution.

Tiered strategy (per the plan): official oEmbed where available -> OpenGraph /
Twitter-card / <title> parse of the canonical page -> Discord-embed fallback ->
give up (none). The posted URL always stays canonical regardless of which tier
produced the metadata. Bluesky links are skipped here (resolved natively at
publish time). Mirrors/scraping/auth remain extension points for later.
"""

from .fetch import ResolvedMetadata, resolve, resolve_bluesky_at_uri

__all__ = ["ResolvedMetadata", "resolve", "resolve_bluesky_at_uri"]
