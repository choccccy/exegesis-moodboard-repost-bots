"""Typed configuration loaded from the environment.

Secrets are resolved at deploy time by `op run --env-file op.env` - nothing is
ever written to disk. See `op.env` for the field reference.
"""

from __future__ import annotations

import json
from functools import lru_cache

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class BoardConfig(BaseModel):
    """One moodboard == one watched Discord channel == one Bluesky account (later)."""

    name: str
    discord_guild_id: int
    discord_channel_id: int
    nsfw: bool = False
    curator_role_ids: list[int] = Field(default_factory=list)
    curator_user_ids: list[int] = Field(default_factory=list)
    # When true, every submission must get an explicit graphic yes/no before it
    # can become ready_to_queue. Set false per board to skip the graphic prompt.
    require_graphic_classification: bool = True
    # Bluesky handle for this board's account (e.g. "robot-posting.bsky.social").
    # App password is supplied separately via BSKY_APP_PASSWORD_<BOARD_NAME_UPPER>.
    bluesky_handle: str | None = None
    # Hashtags appended to every post from this board (no leading #).
    # Start with the channel name; extend after analyzing tag performance.
    tags: list[str] = Field(default_factory=list)
    youtube_playlist_id: str | None = None
    display_name: str | None = None


class Settings(BaseSettings):
    """Process-wide settings. Values come from the environment / generated .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    discord_bot_token: str = Field(..., alias="DISCORD_BOT_TOKEN")
    boards_json: str = Field("[]", alias="BOARDS_JSON")
    trigger_emoji: str = Field("🦋", alias="TRIGGER_EMOJI")

    data_dir: str = Field("/data", alias="DATA_DIR")
    database_url: str = Field(
        "sqlite+aiosqlite:////data/db/bot.db", alias="DATABASE_URL"
    )
    storage_min_free_mb: int = Field(500, alias="STORAGE_MIN_FREE_MB")
    log_level: str = Field("INFO", alias="LOG_LEVEL")

    # Startup catch-up: reconcile 🦋 reactions added while the bot was offline.
    # Discord has no "reactions since X" API, so we scan recent history per
    # channel and ingest any message currently bearing the trigger. Bounded by
    # both a time window and a per-channel message cap to keep API use sane.
    catchup_enabled: bool = Field(True, alias="CATCHUP_ENABLED")
    catchup_lookback_hours: int = Field(168, alias="CATCHUP_LOOKBACK_HOURS")
    catchup_max_messages: int = Field(500, alias="CATCHUP_MAX_MESSAGES")

    # Queue / posting cadence.
    # Posts fire at most once per hour, starting at queue_start_hour in the given timezone.
    # Fresh submissions (created within queue_fresh_window_hours) get a higher daily cap.
    queue_timezone: str = Field("America/Denver", alias="QUEUE_TIMEZONE")
    queue_start_hour: int = Field(12, alias="QUEUE_START_HOUR")
    queue_fresh_window_hours: int = Field(72, alias="QUEUE_FRESH_WINDOW_HOURS")
    queue_fresh_daily_cap: int = Field(6, alias="QUEUE_FRESH_DAILY_CAP")
    queue_backlog_daily_cap: int = Field(3, alias="QUEUE_BACKLOG_DAILY_CAP")

    # Per-board Bluesky app passwords. Named BSKY_APP_PASSWORD_<BOARD_NAME_UPPER>
    # where BOARD_NAME_UPPER is the board name uppercased with hyphens replaced by underscores.
    # e.g. board "robot-posting" -> BSKY_APP_PASSWORD_ROBOT_POSTING
    bsky_app_password_robot_posting: str | None = Field(None, alias="BSKY_APP_PASSWORD_ROBOT_POSTING")
    bsky_app_password_robot_fucking: str | None = Field(None, alias="BSKY_APP_PASSWORD_ROBOT_FUCKING")
    bsky_app_password_weird_wheels: str | None = Field(None, alias="BSKY_APP_PASSWORD_WEIRD_WHEELS")
    bsky_app_password_doohickey_posting: str | None = Field(None, alias="BSKY_APP_PASSWORD_DOOHICKEY_POSTING")
    bsky_app_password_on_topic_memes: str | None = Field(None, alias="BSKY_APP_PASSWORD_MEMES")
    bsky_app_password_nerd_tv: str | None = Field(None, alias="BSKY_APP_PASSWORD_NERD_TV")

    # Optional YouTube Data API v3 key (no OAuth - read-only metadata only).
    # Without this, YouTube video metadata falls back to scraping, which fails
    # from datacenter IPs because YouTube serves JS-only pages to bot UAs.
    youtube_api_key: str | None = Field(None, alias="YOUTUBE_API_KEY")

    # YouTube OAuth2 credentials for playlist writes (separate from the read-only API key).
    youtube_client_id: str | None = Field(None, alias="YOUTUBE_CLIENT_ID")
    youtube_client_secret: str | None = Field(None, alias="YOUTUBE_CLIENT_SECRET")
    youtube_refresh_token: str | None = Field(None, alias="YOUTUBE_REFRESH_TOKEN")

    dashboard_url: str | None = Field(None, alias="DASHBOARD_URL")

    @field_validator("discord_bot_token")
    @classmethod
    def _reject_unresolved_op_ref(cls, value: str) -> str:
        if value.startswith("op://"):
            raise ValueError(
                "DISCORD_BOT_TOKEN is an unresolved 1Password reference. "
                "Deploy with: op run --env-file op.env --no-masking -- ..."
            )
        return value

    @field_validator("boards_json")
    @classmethod
    def _validate_boards_json(cls, value: str) -> str:
        # Fail fast at startup if boards JSON is malformed or fails schema validation.
        boards = json.loads(value)
        for b in boards:
            BoardConfig.model_validate(b)
        return value

    @property
    def boards(self) -> list[BoardConfig]:
        return [BoardConfig.model_validate(b) for b in json.loads(self.boards_json)]

    def board_for_channel(self, channel_id: int) -> BoardConfig | None:
        return next(
            (b for b in self.boards if b.discord_channel_id == channel_id), None
        )

    def bsky_password_for(self, board_name: str) -> str | None:
        """Look up the app password for a board by its name."""
        key = board_name.lower().replace("-", "_")
        return getattr(self, f"bsky_app_password_{key}", None)

    @property
    def attachments_dir(self) -> str:
        return f"{self.data_dir.rstrip('/')}/attachments"

    @property
    def logs_dir(self) -> str:
        return f"{self.data_dir.rstrip('/')}/logs"


@lru_cache
def get_settings() -> Settings:
    """Cached singleton so config is parsed/validated exactly once."""
    return Settings()  # type: ignore[call-arg]
