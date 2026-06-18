"""Persist Discord attachments to a local volume.

Discord CDN URLs are not durable references (they expire / require refresh), so
the bot downloads attachments promptly into a mounted data volume and uses the
stored copies for all later work.
"""

from .store import (
    StorageFullError,
    download_attachment,
    has_free_space,
    submission_dir,
)

__all__ = [
    "StorageFullError",
    "download_attachment",
    "has_free_space",
    "submission_dir",
]
