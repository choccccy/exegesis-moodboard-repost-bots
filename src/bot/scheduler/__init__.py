"""Periodic housekeeping (Milestone 1: storage check + heartbeat).

Runs as an asyncio task inside the single bot process. Later milestones add queue
evaluation, publish retries, and reporting here.
"""

from __future__ import annotations

import asyncio
import logging

from ..asset_store import has_free_space
from ..config import Settings

log = logging.getLogger(__name__)

_HEARTBEAT_SECONDS = 300


async def run_housekeeping(settings: Settings, stop: asyncio.Event) -> None:
    """Loop until ``stop`` is set, logging storage health periodically."""
    while not stop.is_set():
        ok = has_free_space(settings.data_dir, settings.storage_min_free_mb)
        if not ok:
            log.warning(
                "storage below %s MB floor at %s - new attachment downloads will be blocked",
                settings.storage_min_free_mb,
                settings.data_dir,
            )
        else:
            log.debug("heartbeat: storage ok")
        try:
            await asyncio.wait_for(stop.wait(), timeout=_HEARTBEAT_SECONDS)
        except asyncio.TimeoutError:
            continue
