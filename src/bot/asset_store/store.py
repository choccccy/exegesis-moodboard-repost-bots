"""Download + persist attachments; guard against a full volume."""

from __future__ import annotations

import os
import shutil

import httpx


class StorageFullError(RuntimeError):
    """Raised when free space is below the configured floor before a download."""


def has_free_space(data_dir: str, min_free_mb: int) -> bool:
    try:
        usage = shutil.disk_usage(data_dir)
    except OSError:
        return False
    return usage.free >= min_free_mb * 1024 * 1024


def submission_dir(attachments_dir: str, board_id: int, submission_id: int) -> str:
    """Stable per-submission directory: <attachments>/<board>/<submission>/."""
    path = os.path.join(attachments_dir, str(board_id), str(submission_id))
    os.makedirs(path, exist_ok=True)
    return path


def _safe_filename(name: str) -> str:
    base = os.path.basename(name) or "attachment"
    return "".join(ch for ch in base if ch.isalnum() or ch in "._- ").strip() or "attachment"


async def download_attachment(
    *,
    url: str,
    dest_dir: str,
    filename: str,
    data_dir: str,
    min_free_mb: int,
    client: httpx.AsyncClient | None = None,
) -> str:
    """Download ``url`` into ``dest_dir`` and return the local path.

    Raises StorageFullError if the volume is below its free-space floor so the
    caller can mark the submission blocked-by-storage rather than lose state.
    """
    if not has_free_space(data_dir, min_free_mb):
        raise StorageFullError(f"free space below {min_free_mb} MB at {data_dir}")

    dest = os.path.join(dest_dir, _safe_filename(filename))
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=60.0, follow_redirects=True)
    try:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            tmp = dest + ".part"
            with open(tmp, "wb") as fh:
                async for chunk in resp.aiter_bytes():
                    fh.write(chunk)
            os.replace(tmp, dest)
    finally:
        if owns_client:
            await client.aclose()
    return dest
