"""Bluesky publisher - Milestone 2.

Handles external-link card posts and image posts. Bluesky-native reposts
(app.bsky.embed.record) and multi-link reply threads are M3.
"""

from __future__ import annotations

import logging
import mimetypes
from dataclasses import dataclass
from pathlib import Path

from atproto import AsyncClient, models

from ..config import BoardConfig
from ..models import Attachment, Submission, SubmissionLink
from ..state import GraphicStatus

log = logging.getLogger(__name__)

_MAX_GRAPHEMES = 300


@dataclass
class PublishResult:
    success: bool
    at_uri: str | None = None
    at_cid: str | None = None
    error: str | None = None


async def publish_submission(
    submission: Submission,
    links: list[SubmissionLink],
    attachments: list[Attachment],
    board_cfg: BoardConfig,
    password: str,
) -> PublishResult:
    """Authenticate to Bluesky and post the submission. Returns an audit result."""
    if not board_cfg.bluesky_handle:
        return PublishResult(success=False, error="no bluesky_handle configured for board")

    client = AsyncClient()
    try:
        await client.login(board_cfg.bluesky_handle, password)
    except Exception as exc:
        log.error("Bluesky login failed for board %s: %s", board_cfg.name, exc)
        return PublishResult(success=False, error=f"login failed: {exc}")

    has_uploaded_images = any(a.is_image for a in attachments)
    kind = _determine_kind(links, has_uploaded_images)
    labels = _build_labels(submission, board_cfg)

    try:
        if kind == "external":
            return await _publish_external(client, links, labels)
        if kind == "images":
            return await _publish_images(client, links, attachments, labels)
        return PublishResult(success=False, error=f"unsupported embed kind: {kind}")
    except Exception as exc:
        log.error("publish failed for submission %s: %s", submission.id, exc)
        return PublishResult(success=False, error=str(exc))


def _determine_kind(links: list[SubmissionLink], has_uploaded_image: bool) -> str:
    first_family = links[0].domain_family if links else None
    if first_family == "bluesky":
        return "record"
    if has_uploaded_image:
        return "images"
    if links:
        return "external"
    return "empty"


def _build_labels(submission: Submission, board_cfg: BoardConfig):
    vals = []
    if board_cfg.nsfw:
        vals.append(models.ComAtprotoLabelDefs.SelfLabel(val="sexual"))
    if submission.graphic_status == GraphicStatus.GRAPHIC.value:
        vals.append(models.ComAtprotoLabelDefs.SelfLabel(val="graphic-media"))
    return models.ComAtprotoLabelDefs.SelfLabels(values=vals) if vals else None


def _post_text_and_facets(title: str | None, url: str) -> tuple[str, list]:
    """Build post text containing title + URL, with a link facet on the URL."""
    if title:
        max_title = _MAX_GRAPHEMES - len(url) - 1  # -1 for newline
        if len(title) > max_title:
            title = title[: max_title - 1] + "..."
        text = f"{title}\n{url}"
    else:
        text = url

    # Byte-offset facet so the URL is a clickable link in Bluesky clients.
    text_bytes = text.encode("utf-8")
    url_bytes = url.encode("utf-8")
    start = text_bytes.rfind(url_bytes)
    end = start + len(url_bytes)
    facets = [
        models.AppBskyRichtextFacet.Main(
            features=[models.AppBskyRichtextFacet.Link(uri=url)],
            index=models.AppBskyRichtextFacet.ByteSlice(byte_start=start, byte_end=end),
        )
    ]
    return text, facets


async def _upload_blob(client: AsyncClient, path: str) -> object | None:
    """Read a local file and upload it as a blob. Returns the blob ref or None."""
    try:
        data = Path(path).read_bytes()
        mime, _ = mimetypes.guess_type(path)
        response = await client.upload_blob(data)
        return response.blob
    except Exception as exc:
        log.warning("blob upload failed for %s: %s", path, exc)
        return None


async def _publish_external(
    client: AsyncClient,
    links: list[SubmissionLink],
    labels,
) -> PublishResult:
    primary = links[0]
    url = primary.canonical_url
    title = primary.resolved_title
    description = primary.resolved_description or ""

    thumb_blob = None
    if primary.resolved_image_path:
        thumb_blob = await _upload_blob(client, primary.resolved_image_path)

    embed = models.AppBskyEmbedExternal.Main(
        external=models.AppBskyEmbedExternal.External(
            uri=url,
            title=title or url,
            description=description,
            thumb=thumb_blob,
        )
    )
    text, facets = _post_text_and_facets(title, url)
    response = await client.send_post(
        text=text, facets=facets, embed=embed, labels=labels, langs=["en"]
    )
    return PublishResult(success=True, at_uri=response.uri, at_cid=response.cid)


async def _publish_images(
    client: AsyncClient,
    links: list[SubmissionLink],
    attachments: list[Attachment],
    labels,
) -> PublishResult:
    images = []
    for att in (a for a in attachments if a.is_image):
        if not att.local_path:
            log.warning("image attachment %s has no local_path, skipping", att.id)
            continue
        blob = await _upload_blob(client, att.local_path)
        if blob is None:
            continue
        images.append(
            models.AppBskyEmbedImages.Image(
                image=blob,
                alt=att.alt_text_body or "",
            )
        )

    if not images:
        return PublishResult(success=False, error="no images could be uploaded")

    embed = models.AppBskyEmbedImages.Main(images=images)
    primary = links[0] if links else None
    url = primary.canonical_url if primary else None
    title = primary.resolved_title if primary else None

    if url:
        text, facets = _post_text_and_facets(title, url)
    else:
        text, facets = "", []

    response = await client.send_post(
        text=text,
        facets=facets or None,
        embed=embed,
        labels=labels,
        langs=["en"],
    )
    return PublishResult(success=True, at_uri=response.uri, at_cid=response.cid)


def at_uri_to_url(at_uri: str) -> str:
    """Convert an AT URI to a bsky.app web URL for display."""
    # at://did:plc:xxx/app.bsky.feed.post/rkey -> https://bsky.app/profile/did:plc:xxx/post/rkey
    parts = at_uri.removeprefix("at://").split("/")
    if len(parts) >= 3:
        return f"https://bsky.app/profile/{parts[0]}/post/{parts[2]}"
    return at_uri
