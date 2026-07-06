"""Tests for typed settings (bot.config) and the version fallback (bot.version)."""

from __future__ import annotations

import importlib
import json
from importlib.metadata import PackageNotFoundError
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from bot.config import BoardConfig, Settings, get_settings

VALID_TOKEN = "x" * 60

BOARDS = json.dumps(
    [
        {
            "name": "robot-posting",
            "discord_guild_id": 111,
            "discord_channel_id": 100,
        },
        {
            "name": "weird-wheels",
            "discord_guild_id": 111,
            "discord_channel_id": 200,
            "nsfw": True,
            "tags": ["vehicles"],
        },
    ]
)

_ENV_VARS = (
    "DISCORD_BOT_TOKEN",
    "BOARDS_JSON",
    "TRIGGER_EMOJI",
    "DATA_DIR",
    "BSKY_APP_PASSWORD_ROBOT_POSTING",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Keep host environment variables from leaking into Settings under test."""
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def make_settings(**overrides) -> Settings:
    kwargs = {"DISCORD_BOT_TOKEN": VALID_TOKEN, "BOARDS_JSON": BOARDS}
    kwargs.update(overrides)
    return Settings(_env_file=None, **kwargs)


def test_token_validator_rejects_unresolved_op_ref():
    with pytest.raises(ValidationError, match="1Password"):
        make_settings(DISCORD_BOT_TOKEN="op://vault/discord/token")


def test_token_validator_accepts_normal_token():
    assert make_settings().discord_bot_token == VALID_TOKEN


def test_boards_json_rejects_malformed_json():
    with pytest.raises(ValidationError):
        make_settings(BOARDS_JSON="[not json")


def test_boards_json_rejects_schema_violation():
    # discord_channel_id is required by BoardConfig.
    bad = json.dumps([{"name": "robots", "discord_guild_id": 1}])
    with pytest.raises(ValidationError):
        make_settings(BOARDS_JSON=bad)


def test_boards_property_parses_board_configs():
    boards = make_settings().boards
    assert [type(b) for b in boards] == [BoardConfig, BoardConfig]
    assert boards[0].name == "robot-posting"
    assert boards[0].nsfw is False
    assert boards[1].nsfw is True
    assert boards[1].tags == ["vehicles"]


def test_boards_default_is_empty_list():
    assert make_settings(BOARDS_JSON="[]").boards == []


def test_board_for_channel_hit():
    board = make_settings().board_for_channel(200)
    assert board is not None
    assert board.name == "weird-wheels"


def test_board_for_channel_miss_returns_none():
    assert make_settings().board_for_channel(999) is None


def test_bsky_password_for_maps_hyphenated_board_name():
    settings = make_settings(BSKY_APP_PASSWORD_ROBOT_POSTING="hunter2")
    assert settings.bsky_password_for("robot-posting") == "hunter2"


def test_bsky_password_for_unset_board_is_none():
    assert make_settings().bsky_password_for("weird-wheels") is None


def test_bsky_password_for_unknown_board_returns_none():
    assert make_settings().bsky_password_for("no-such-board") is None


def test_attachments_and_logs_dirs_derive_from_data_dir():
    settings = make_settings(DATA_DIR="/srv/bot")
    assert settings.attachments_dir == "/srv/bot/attachments"
    assert settings.logs_dir == "/srv/bot/logs"


def test_dirs_handle_trailing_slash():
    settings = make_settings(DATA_DIR="/srv/bot/")
    assert settings.attachments_dir == "/srv/bot/attachments"
    assert settings.logs_dir == "/srv/bot/logs"


def test_defaults():
    settings = make_settings()
    assert settings.trigger_emoji == "🦋"
    assert settings.data_dir == "/data"
    assert settings.storage_min_free_mb == 500


def test_get_settings_is_cached(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no .env in cwd
    monkeypatch.setenv("DISCORD_BOT_TOKEN", VALID_TOKEN)
    get_settings.cache_clear()
    try:
        first = get_settings()
        assert get_settings() is first
        assert first.discord_bot_token == VALID_TOKEN
    finally:
        get_settings.cache_clear()


def test_version_falls_back_to_dev_when_package_missing():
    import bot.version

    try:
        with patch("importlib.metadata.version", side_effect=PackageNotFoundError):
            importlib.reload(bot.version)
            assert bot.version.__version__ == "dev"
    finally:
        importlib.reload(bot.version)
    assert bot.version.__version__ != ""
