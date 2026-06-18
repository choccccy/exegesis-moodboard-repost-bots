"""Discord ingestion surface (Milestone 1 source of truth).

Watches configured channels for the 🦋 trigger, creates submissions, downloads
attachments, and runs the procedural request/reply loop. The submission model it
writes is platform-neutral so a Matrix adapter can mirror this behavior later.
"""

from .client import RepostBot

__all__ = ["RepostBot"]
