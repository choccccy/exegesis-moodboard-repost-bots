"""Tests for configure_logging in bot.logging_setup.

Covers: the logs directory is created and a file handler writes into it, the
requested level is applied to the root logger (with an INFO fallback for
unknown names), and repeat calls do not stack handlers (basicConfig is called
with force=True, which replaces the previous handlers).

Logging configuration is process-global, so a fixture detaches the root
logger's existing handlers before each test (without closing them, since
force=True would close whatever is attached) and restores handlers and level
afterwards, closing any handlers the test itself created.
"""

import logging

import pytest

from bot.logging_setup import configure_logging


@pytest.fixture(autouse=True)
def restore_root_logging():
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    saved_discord = logging.getLogger("discord").level
    saved_discord_http = logging.getLogger("discord.http").level
    # Detach (do not close) pre-existing handlers so basicConfig(force=True)
    # inside configure_logging cannot close pytest's capture handlers.
    root.handlers.clear()
    yield
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        handler.close()
    for handler in saved_handlers:
        root.addHandler(handler)
    root.setLevel(saved_level)
    logging.getLogger("discord").setLevel(saved_discord)
    logging.getLogger("discord.http").setLevel(saved_discord_http)


def test_configure_logging_creates_dir_and_writes_log_file(tmp_path):
    logs_dir = tmp_path / "logs"
    assert not logs_dir.exists()

    configure_logging("info", str(logs_dir))

    assert logs_dir.is_dir()
    root = logging.getLogger()
    file_handlers = [h for h in root.handlers if isinstance(h, logging.FileHandler)]
    assert len(file_handlers) == 1
    assert file_handlers[0].baseFilename == str(logs_dir / "bot.log")

    logging.getLogger("test.logging_setup").info("hello volume")
    file_handlers[0].flush()
    assert "hello volume" in (logs_dir / "bot.log").read_text()


def test_configure_logging_applies_level_with_info_fallback(tmp_path):
    configure_logging("debug", str(tmp_path / "logs"))
    assert logging.getLogger().level == logging.DEBUG

    configure_logging("not-a-level", str(tmp_path / "logs2"))
    assert logging.getLogger().level == logging.INFO


def test_configure_logging_second_call_does_not_stack_handlers(tmp_path):
    configure_logging("info", str(tmp_path / "logs"))
    first_count = len(logging.getLogger().handlers)

    configure_logging("info", str(tmp_path / "logs"))

    # force=True replaces the previous handlers, so the count stays flat:
    # one stdout StreamHandler plus one FileHandler.
    root = logging.getLogger()
    assert len(root.handlers) == first_count == 2
    assert len([h for h in root.handlers if isinstance(h, logging.FileHandler)]) == 1
