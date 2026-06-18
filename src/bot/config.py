"""Typed configuration loaded from the environment.

The real environment is produced at deploy time by `op inject -i .env.tmpl -o .env`
so no secret ever lives in source. See `.env.tmpl` for the field reference.
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

    @field_validator("boards_json")
    @classmethod
    def _validate_boards_json(cls, value: str) -> str:
        # Fail fast at startup if the operator's board JSON is malformed.
        json.loads(value)
        return value

    @property
    def boards(self) -> list[BoardConfig]:
        return [BoardConfig.model_validate(b) for b in json.loads(self.boards_json)]

    def board_for_channel(self, channel_id: int) -> BoardConfig | None:
        return next(
            (b for b in self.boards if b.discord_channel_id == channel_id), None
        )

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
