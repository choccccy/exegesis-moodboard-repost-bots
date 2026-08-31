"""The feed registry - single source of truth for which feeds this service serves.

Keyed by rkey (the record key that forms each feed's permanent AT-URI:
``at://<owner-did>/app.bsky.feed.generator/<rkey>``). Both the record publisher
(``bot.admin.publish_feedgen``) and the runtime skeleton endpoint read from here, so
adding a feed is a one-line change in one place.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FeedDef:
    rkey: str
    display_name: str
    description: str
    # Whether posts from NSFW boards (Board.nsfw) may appear in this feed.
    nsfw_allowed: bool


FEEDS: dict[str, FeedDef] = {
    "exegesis_moodboards": FeedDef(
        rkey="exegesis_moodboards",
        display_name="Exegesis - Everything",
        description="Every Exegesis moodboard post.",
        nsfw_allowed=True,
    ),
    "exegesis_sfw_moodboards": FeedDef(
        rkey="exegesis_sfw_moodboards",
        display_name="Exegesis - SFW",
        description="Exegesis moodboards, safe-for-work boards only.",
        nsfw_allowed=False,
    ),
}
