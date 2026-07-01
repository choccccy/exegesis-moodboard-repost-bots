"""Lightweight bot status file written by the bot process, read by the dashboard.

The bot writes /data/bot_status.json on startup and whenever a Discord rate
limit is observed. The dashboard reads it on each page load - no DB needed.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_STATUS_FILE: Path | None = None
_status: dict = {}

_RETRY_RE = re.compile(r"Retrying in ([\d.]+) seconds")
_ROUTE_RE = re.compile(r"(GET|POST|PUT|PATCH|DELETE)\s+(https?://\S+)\s+responded")


def init(data_dir: str) -> None:
    """Call once at bot startup to initialise the status file."""
    global _STATUS_FILE, _status
    _STATUS_FILE = Path(data_dir) / "bot_status.json"
    _status = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "rate_limit": None,
    }
    _write()
    logging.getLogger("discord.http").addHandler(_RateLimitHandler())


def _write() -> None:
    if _STATUS_FILE is None:
        return
    try:
        _STATUS_FILE.write_text(json.dumps(_status))
    except OSError as exc:
        log.warning("could not write bot_status.json: %s", exc)


def _record_rate_limit(retry_after: float, route: str) -> None:
    now = time.time()
    _status["rate_limit"] = {
        "until": now + retry_after,
        "route": route,
        "retry_after": retry_after,
        "last_seen_at": now,  # persists so dashboard can show "was rate limited recently"
    }
    _write()


def record_thread_count(active: int) -> None:
    """Write the current Discord active-thread count, as reported by the API."""
    _status["discord_active_threads"] = active
    _write()


def scan_started(channel_id: int, channel_name: str, scan_type: str = "catchup") -> None:
    """Record that a channel scan is now active (catchup or /scan)."""
    scans: list = _status.setdefault("active_scans", [])
    if not any(s.get("channel_id") == channel_id for s in scans):
        scans.append({
            "channel_id": channel_id,
            "channel_name": channel_name,
            "type": scan_type,
            "started_at": time.time(),
        })
    _write()


def scan_finished(channel_id: int) -> None:
    """Remove a channel scan from the active list."""
    _status["active_scans"] = [
        s for s in _status.get("active_scans", [])
        if s.get("channel_id") != channel_id
    ]
    _write()


class _RateLimitHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        if "rate limit" not in msg.lower():
            return
        retry_m = _RETRY_RE.search(msg)
        route_m = _ROUTE_RE.search(msg)
        retry_after = float(retry_m.group(1)) if retry_m else 30.0
        route = route_m.group(2) if route_m else "unknown"
        _record_rate_limit(retry_after, route)


def read(data_dir: str) -> dict:
    """Read the status file; returns empty dict if missing or unreadable."""
    path = Path(data_dir) / "bot_status.json"
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}
