"""Per-image alt-text requirements and fulfillment.

Every non-Bluesky-native uploaded image needs alt text before a submission can
move to ready_to_queue. Discord exposes an attachment ``description`` field in
some clients; if present we reuse it instead of asking a human again.
"""

from __future__ import annotations

from ..state import AltTextStatus

# Discord image content types we treat as requiring alt text.
IMAGE_MIME_PREFIX = "image/"
VIDEO_MIME_PREFIX = "video/"
_VIDEO_EXTS = (".mp4", ".mov", ".webm", ".avi", ".mkv", ".m4v")


def is_image_attachment(content_type: str | None, filename: str) -> bool:
    if content_type and content_type.startswith(IMAGE_MIME_PREFIX):
        return True
    lowered = filename.lower()
    return lowered.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff"))


def is_video_attachment(content_type: str | None, filename: str) -> bool:
    if content_type and content_type.startswith(VIDEO_MIME_PREFIX):
        return True
    return filename.lower().endswith(_VIDEO_EXTS)


def initial_alt_text(
    *, is_image: bool, is_video: bool = False, discord_description: str | None
) -> tuple[AltTextStatus, str | None]:
    """Decide the starting alt-text state for a freshly seen attachment."""
    if not (is_image or is_video):
        return AltTextStatus.NOT_REQUIRED, None
    description = (discord_description or "").strip()
    if description:
        return AltTextStatus.PROVIDED, description
    return AltTextStatus.NEEDED, None
