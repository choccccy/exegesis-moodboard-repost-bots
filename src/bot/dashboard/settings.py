"""Minimal settings for the dashboard process - only needs DB + board config."""

from __future__ import annotations

import json

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from ..config import BoardConfig


class DashboardSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = Field("sqlite+aiosqlite:////data/db/bot.db", alias="DATABASE_URL")
    data_dir: str = Field("/data", alias="DATA_DIR")
    boards_json: str = Field("[]", alias="BOARDS_JSON")

    queue_timezone: str = Field("America/Denver", alias="QUEUE_TIMEZONE")
    queue_fresh_window_hours: int = Field(72, alias="QUEUE_FRESH_WINDOW_HOURS")
    queue_fresh_daily_cap: int = Field(6, alias="QUEUE_FRESH_DAILY_CAP")
    queue_backlog_daily_cap: int = Field(3, alias="QUEUE_BACKLOG_DAILY_CAP")

    @property
    def boards(self) -> list[BoardConfig]:
        return [BoardConfig.model_validate(b) for b in json.loads(self.boards_json)]

    def bluesky_handle_for(self, board_name: str) -> str | None:
        return next((b.bluesky_handle for b in self.boards if b.name == board_name), None)
