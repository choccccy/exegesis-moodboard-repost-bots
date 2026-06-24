"""Structured-ish logging to stdout and to a file on the persistent volume."""

from __future__ import annotations

import logging
import os
import sys

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s :: %(message)s"


def configure_logging(level: str, logs_dir: str) -> None:
    """Configure root logging once. Safe to call at startup.

    Logs go to stdout (captured by `docker compose logs`) and to a rotating-free
    flat file on the mounted volume so operators have durable history.
    """
    os.makedirs(logs_dir, exist_ok=True)

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(logging.FileHandler(os.path.join(logs_dir, "bot.log")))
    except OSError:
        # If the volume is unwritable we still want stdout logging to work.
        pass

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=_FORMAT,
        handlers=handlers,
        force=True,
    )

    # discord.py is chatty at INFO for the gateway; keep it at WARNING.
    # discord.http logs rate-limit sleeps at WARNING - keep those visible.
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.WARNING)
