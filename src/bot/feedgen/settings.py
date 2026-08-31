"""Minimal settings for the feed-generator process - only needs DB + identity.

Unlike the dashboard, the feed generator needs no board config (skeletons are bare
post URIs; Bluesky renders them) and holds no Bluesky credentials. It only needs the
database to read from and its own service identity.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class FeedgenSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = Field("sqlite+aiosqlite:////data/db/bot.db", alias="DATABASE_URL")

    # Public hostname this service is served under; also the did:web anchor.
    service_hostname: str = Field("feed.exegesis.space", alias="FEEDGEN_SERVICE_HOSTNAME")
    # DID of the account whose repo holds the app.bsky.feed.generator records
    # (choccy.gay). Used only to build the feed AT-URIs in describeFeedGenerator.
    owner_did: str = Field(..., alias="FEEDGEN_OWNER_DID")

    @property
    def service_did(self) -> str:
        return f"did:web:{self.service_hostname}"

    def feed_uri(self, rkey: str) -> str:
        return f"at://{self.owner_did}/app.bsky.feed.generator/{rkey}"
