"""Bluesky publisher - Milestones 2 & 3.

M2: external-link card posts and image posts.
M3: native Bluesky reposts and multi-link reply threads.
"""

from __future__ import annotations

import datetime
import logging
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from atproto import AsyncClient, models

from ..config import BoardConfig
from ..models import Attachment, Submission, SubmissionLink
from ..state import GraphicStatus

log = logging.getLogger(__name__)

_MAX_GRAPHEMES = 300

# Post text for a media post whose source was explicitly waived (no findable source).
_SOURCE_UNKNOWN_TEXT = "source unknown"


def _sourceless_text(source_note: str | None) -> str:
    """Root-post text for a media post with no canonical link: a confirmed free-text
    source ("source: Popular Mechanics, March 1965") or the waived fallback."""
    return f"source: {source_note}" if source_note else _SOURCE_UNKNOWN_TEXT

# Matches Bluesky hashtags: # followed by at least one letter (not a bare digit-only tag),
# then any word characters. Negative lookbehind avoids matching inside URLs (#anchor).
_TAG_RE = re.compile(r"(?<!\w)#([a-zA-Z][a-zA-Z0-9_]*)(?!\w)")


@dataclass
class PublishResult:
    success: bool
    at_uri: str | None = None
    at_cid: str | None = None
    error: str | None = None
    bsky_url: str | None = None
    is_repost: bool = False
    bsky_root_uri: str | None = None
    bsky_root_cid: str | None = None


async def publish_submission(
    submission: Submission,
    links: list[SubmissionLink],
    attachments: list[Attachment],
    board_cfg: BoardConfig,
    password: str,
    *,
    reply_parent_uri: str | None = None,
    reply_parent_cid: str | None = None,
    reply_root_uri: str | None = None,
    reply_root_cid: str | None = None,
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
    has_uploaded_video = any(a.is_video for a in attachments)
    kind = _determine_kind(links, has_uploaded_images, has_uploaded_video)
    labels = _build_labels(submission, board_cfg)
    all_tags = list(board_cfg.tags)
    if board_cfg.nsfw:
        all_tags.append("nsfw")
    source_note = submission.source_note if submission.source_note_confirmed else None

    reply = None
    if kind != "record" and reply_parent_uri and reply_parent_cid and reply_root_uri and reply_root_cid:
        reply = models.AppBskyFeedPost.ReplyRef(
            root=models.ComAtprotoRepoStrongRef.Main(uri=reply_root_uri, cid=reply_root_cid),
            parent=models.ComAtprotoRepoStrongRef.Main(uri=reply_parent_uri, cid=reply_parent_cid),
        )

    try:
        if kind == "record":
            result = await _publish_record(client, links)
        elif kind == "external":
            result = await _publish_external(client, links, labels, all_tags, reply=reply)
        elif kind == "images":
            result = await _publish_images(client, links, attachments, labels, all_tags, reply=reply, source_note=source_note)
        elif kind == "video":
            result = await _publish_video(client, links, attachments, labels, all_tags, reply=reply, source_note=source_note)
        else:
            return PublishResult(success=False, error=f"unsupported embed kind: {kind}")
    except Exception as exc:
        log.error("publish failed for submission %s: %s", submission.id, exc)
        return PublishResult(success=False, error=str(exc))

    if result.success and result.at_uri:
        result.bsky_root_uri = reply_root_uri or result.at_uri
        result.bsky_root_cid = reply_root_cid or result.at_cid
        if kind == "record":
            # For native reposts, show the original post's URL, not the repost record.
            result.bsky_url = links[0].canonical_url
        else:
            await _like_quietly(client, result.at_uri, result.at_cid)
            result.bsky_url = at_uri_to_url(result.at_uri, board_cfg.bluesky_handle)
            reply_uri, reply_cid = result.at_uri, result.at_cid
            if kind == "video":
                videos = [a for a in attachments if a.is_video]
                images = [a for a in attachments if a.is_image]
                for extra_video in videos[1:]:
                    try:
                        reply_uri, reply_cid = await _publish_video_reply(
                            client, extra_video, links, labels, all_tags,
                            result.at_uri, result.at_cid, reply_uri, reply_cid,
                        )
                    except Exception as exc:
                        log.warning("extra video reply failed for submission %s: %s", submission.id, exc)
                        break
                if images:
                    try:
                        reply_uri, reply_cid = await _publish_image_reply(
                            client, images, links, labels, all_tags,
                            result.at_uri, result.at_cid, reply_uri, reply_cid,
                        )
                    except Exception as exc:
                        log.warning("image reply failed for submission %s: %s", submission.id, exc)
            if len(links) > 1:
                await _publish_reply_thread(
                    client, links[1:], labels, all_tags,
                    result.at_uri, result.at_cid, submission.id,
                    parent_uri=reply_uri, parent_cid=reply_cid,
                )

    return result


def _determine_kind(links: list[SubmissionLink], has_uploaded_image: bool, has_uploaded_video: bool = False) -> str:
    first_family = links[0].domain_family if links else None
    if first_family == "bluesky":
        return "record"
    if has_uploaded_video:
        return "video"
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
    """Build post text containing title + URL, with a link facet on the URL.

    Also adds tag facets for any #hashtags found in the title so they render
    as clickable tags in Bluesky clients.
    """
    if title:
        max_title = _MAX_GRAPHEMES - len(url) - 1  # -1 for newline
        if len(title) > max_title:
            title = title[: max_title - 3] + "..."
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

    # Emit tag facets for #hashtags in the title. The title always starts at
    # byte 0, so byte offsets from scanning it directly match the full text.
    if title:
        for m in _TAG_RE.finditer(title):
            tag = m.group(1)
            tag_start = len(title[: m.start()].encode("utf-8"))
            tag_end = tag_start + len(m.group(0).encode("utf-8"))
            facets.append(
                models.AppBskyRichtextFacet.Main(
                    features=[models.AppBskyRichtextFacet.Tag(tag=tag)],
                    index=models.AppBskyRichtextFacet.ByteSlice(byte_start=tag_start, byte_end=tag_end),
                )
            )

    return text, facets


_BSKY_MAX_BLOB = 975_000  # bytes; Bluesky hard limit is 1,000,000 - stay comfortably under


def _compress_for_bsky(data: bytes) -> bytes:
    """Compress image data to fit within Bluesky's 1MB blob limit.

    Animated GIFs are converted to animated WebP (far smaller) at successively
    lower quality levels, then with halved resolution, before giving up and
    falling through to a static JPEG. Static images go straight to JPEG.
    """
    import io
    from PIL import Image, ImageSequence

    img = Image.open(io.BytesIO(data))

    if img.format == "GIF" and getattr(img, "n_frames", 1) > 1:
        frames: list = []
        durations: list = []
        for frame in ImageSequence.Iterator(img):
            frames.append(frame.convert("RGBA"))
            durations.append(frame.info.get("duration", 100))

        scale = 1.0
        while scale > 0.05:
            new_w = max(1, int(frames[0].width * scale))
            new_h = max(1, int(frames[0].height * scale))
            scaled = (
                frames if scale == 1.0
                else [f.resize((new_w, new_h), Image.LANCZOS) for f in frames]
            )
            for quality in (80, 60, 40, 20):
                buf = io.BytesIO()
                scaled[0].save(
                    buf, format="WEBP", save_all=True, append_images=scaled[1:],
                    duration=durations, loop=0, quality=quality,
                )
                if buf.tell() <= _BSKY_MAX_BLOB:
                    log.info(
                        "compressed animated GIF to WebP: %d bytes (scale=%.2f q=%d)",
                        buf.tell(), scale, quality,
                    )
                    return buf.getvalue()
            scale *= 0.5

        log.warning("animated GIF could not be compressed to WebP within limit; falling back to static JPEG")
        img = frames[0]

    if img.mode in ("RGBA", "P", "LA"):
        img = img.convert("RGB")
    for quality in (85, 70, 55, 40, 25):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        if buf.tell() <= _BSKY_MAX_BLOB:
            log.info("compressed image to %d bytes (quality=%d)", buf.tell(), quality)
            return buf.getvalue()
    # Quality alone wasn't enough - halve resolution until it fits.
    while img.width > 100:
        img = img.resize((img.width // 2, img.height // 2), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=25, optimize=True)
        if buf.tell() <= _BSKY_MAX_BLOB:
            log.info("compressed image to %d bytes (%dx%d)", buf.tell(), img.width, img.height)
            return buf.getvalue()
    return buf.getvalue()


def _error_detail(exc: Exception, limit: int = 300) -> str:
    """Compact one-line description of an exception, for PublishAttempt.error.

    Keeps the leading part of the message (which carries status codes and error
    names) so the dashboard shows the actual cause, not just a generic label.
    """
    detail = " ".join(f"{type(exc).__name__}: {exc}".split())
    return detail if len(detail) <= limit else detail[:limit] + "..."


async def _upload_blob(client: AsyncClient, path: str) -> tuple[object | None, str | None]:
    """Read a local file and upload it as a blob. Returns (blob_ref, error_detail)."""
    try:
        data = Path(path).read_bytes()
        if len(data) > _BSKY_MAX_BLOB:
            log.warning("image %s is %d bytes, compressing before upload", path, len(data))
            data = _compress_for_bsky(data)
        response = await client.upload_blob(data)
        return response.blob, None
    except Exception as exc:
        log.warning("blob upload failed for %s: %s", path, exc)
        return None, _error_detail(exc)


async def _like_quietly(client: AsyncClient, uri: str, cid: str) -> None:
    """Like our own post, best-effort. A failure is logged, never raised, so it
    can't break the publish or the reply chain. The URI identifies the post."""
    try:
        await client.like(uri, cid)
    except Exception as exc:
        log.warning("failed to like own post %s: %s", uri, exc)


async def _external_embed(client: AsyncClient, link: SubmissionLink):
    """Build an external-link embed card for a link, uploading its resolved
    thumbnail if one was downloaded. Shared by the root external post and the
    link-reply posts so replies get the same card as the root."""
    thumb_blob = None
    if link.resolved_image_path:
        thumb_blob, _ = await _upload_blob(client, link.resolved_image_path)  # thumb is optional
    return models.AppBskyEmbedExternal.Main(
        external=models.AppBskyEmbedExternal.External(
            uri=link.canonical_url,
            title=link.resolved_title or link.canonical_url,
            description=link.resolved_description or "",
            thumb=thumb_blob,
        )
    )


async def _create_post(
    client: AsyncClient,
    *,
    text: str,
    facets: list,
    embed,
    labels,
    reply: object = None,
) -> PublishResult:
    """Low-level post creation via create_record so we can attach labels and reply refs."""
    created_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    record = models.AppBskyFeedPost.Record(
        text=text,
        facets=facets or None,
        embed=embed,
        labels=labels,
        reply=reply,
        langs=["en"],
        created_at=created_at,
    )
    response = await client.com.atproto.repo.create_record(
        models.ComAtprotoRepoCreateRecord.Data(
            repo=client.me.did,
            collection=models.ids.AppBskyFeedPost,
            record=record,
        )
    )
    return PublishResult(success=True, at_uri=response.uri, at_cid=response.cid)


async def _cid_for_at_uri(client: AsyncClient, at_uri: str) -> str:
    """Fetch the current cid for an at:// post URI (required alongside the URI to
    repost/like a record). Raises if the post no longer exists."""
    posts_resp = await client.get_posts([at_uri])
    if not posts_resp.posts:
        raise ValueError(f"Bluesky post not found: {at_uri}")
    return posts_resp.posts[0].cid


async def _resolve_bluesky_post(client: AsyncClient, canonical_url: str) -> tuple[str, str]:
    """Resolve a bsky.app URL to (at_uri, cid) by resolving the handle live.

    Handles both handle-based and DID-based profile URLs. Fallback path for links
    captured before we pinned the DID (``source_at_uri``); it fails if the author
    has since renamed or deactivated the handle in the URL.
    """
    parsed = urlparse(canonical_url)
    # path: /profile/{handle_or_did}/post/{rkey}
    parts = parsed.path.strip("/").split("/")
    handle_or_did = parts[1]
    rkey = parts[3]

    if handle_or_did.startswith("did:"):
        did = handle_or_did
    else:
        resp = await client.resolve_handle(handle_or_did)
        did = resp.did

    at_uri = f"at://{did}/app.bsky.feed.post/{rkey}"
    return at_uri, await _cid_for_at_uri(client, at_uri)


async def _publish_record(client: AsyncClient, links: list[SubmissionLink]) -> PublishResult:
    """Create a native Bluesky repost of a bsky.app source link, and like the original."""
    link = links[0]
    try:
        # Prefer the DID-based URI pinned at capture time; it survives source handle
        # renames. Fall back to live handle resolution for links captured earlier.
        if link.source_at_uri:
            at_uri = link.source_at_uri
            cid = await _cid_for_at_uri(client, at_uri)
        else:
            at_uri, cid = await _resolve_bluesky_post(client, link.canonical_url)
    except Exception as exc:
        return PublishResult(success=False, error=f"could not resolve Bluesky post: {exc}")

    response = await client.repost(at_uri, cid)
    try:
        await client.like(at_uri, cid)
    except Exception as exc:
        log.warning("failed to like reposted Bluesky post %s: %s", at_uri, exc)
    return PublishResult(success=True, at_uri=response.uri, at_cid=response.cid, is_repost=True)


async def _publish_reply_thread(
    client: AsyncClient,
    extra_links: list[SubmissionLink],
    labels,
    tags: list[str],
    root_uri: str,
    root_cid: str,
    submission_id: int,
    parent_uri: str | None = None,
    parent_cid: str | None = None,
) -> None:
    """Publish additional links as reply posts chained off the root post."""
    parent_uri = parent_uri or root_uri
    parent_cid = parent_cid or root_cid
    for link in extra_links:
        try:
            reply_uri, reply_cid = await _publish_reply_post(
                client, link, labels, tags,
                root_uri, root_cid, parent_uri, parent_cid,
            )
            parent_uri, parent_cid = reply_uri, reply_cid
        except Exception as exc:
            log.warning(
                "reply post failed for submission %s, link %s: %s",
                submission_id, link.canonical_url, exc,
            )
            break


async def _publish_reply_post(
    client: AsyncClient,
    link: SubmissionLink,
    labels,
    tags: list[str],
    root_uri: str,
    root_cid: str,
    parent_uri: str,
    parent_cid: str,
) -> tuple[str, str]:
    """Create one reply post in a thread. Returns (at_uri, cid)."""
    url = link.canonical_url
    title = link.resolved_title
    text, facets = _post_text_and_facets(title, url)
    text, facets = _append_tags(text, facets, tags)

    embed = await _external_embed(client, link)
    reply_ref = models.AppBskyFeedPost.ReplyRef(
        root=models.ComAtprotoRepoStrongRef.Main(uri=root_uri, cid=root_cid),
        parent=models.ComAtprotoRepoStrongRef.Main(uri=parent_uri, cid=parent_cid),
    )
    result = await _create_post(
        client,
        text=text,
        facets=facets,
        embed=embed,
        labels=labels,
        reply=reply_ref,
    )
    if not result.at_uri or not result.at_cid:
        raise RuntimeError("reply post creation returned no URI/CID")
    await _like_quietly(client, result.at_uri, result.at_cid)
    return result.at_uri, result.at_cid


async def _publish_external(
    client: AsyncClient,
    links: list[SubmissionLink],
    labels,
    tags: list[str],
    *,
    reply=None,
) -> PublishResult:
    primary = links[0]
    url = primary.canonical_url
    title = primary.resolved_title

    embed = await _external_embed(client, primary)
    text, facets = _post_text_and_facets(title, url)
    text, facets = _append_tags(text, facets, tags)
    return await _create_post(client, text=text, facets=facets, embed=embed, labels=labels, reply=reply)


async def _publish_images(
    client: AsyncClient,
    links: list[SubmissionLink],
    attachments: list[Attachment],
    labels,
    tags: list[str],
    *,
    reply=None,
    source_note: str | None = None,
) -> PublishResult:
    images = []
    upload_errors: list[str] = []
    for att in (a for a in attachments if a.is_image):
        if not att.local_path:
            log.warning("image attachment %s has no local_path, skipping", att.id)
            upload_errors.append(f"attachment {att.id} has no local file")
            continue
        blob, err = await _upload_blob(client, att.local_path)
        if blob is None:
            if err:
                upload_errors.append(err)
            continue
        images.append(
            models.AppBskyEmbedImages.Image(
                image=blob,
                alt=att.alt_text_body or "",
            )
        )

    if not images:
        detail = f": {upload_errors[-1]}" if upload_errors else ""
        return PublishResult(success=False, error=f"no images could be uploaded{detail}")

    embed = models.AppBskyEmbedImages.Main(images=images)
    primary = links[0] if links else None
    url = primary.canonical_url if primary else None
    title = primary.resolved_title if primary else None

    if url:
        text, facets = _post_text_and_facets(title, url)
    else:
        # No link: a confirmed non-URL source, or the waived "source unknown" fallback.
        text, facets = _sourceless_text(source_note), []

    text, facets = _append_tags(text, facets, tags)
    return await _create_post(client, text=text, facets=facets, embed=embed, labels=labels, reply=reply)


_BSKY_MAX_VIDEO = 95 * 1024 * 1024  # 95 MB - headroom under Bluesky's 100 MB limit


async def _upload_video_blob(client: AsyncClient, att: Attachment) -> tuple[object | None, str | None]:
    """Upload a local video file as a regular blob. Returns (blob_ref, error_detail).

    Videos go through the same uploadBlob endpoint as images (the path the SDK's
    own send_video uses); the AppView transcodes them after the record is created.
    The job-based app.bsky.video.uploadVideo API lives on a separate service
    (video.bsky.app), not the PDS - calling it via the session client hits the
    PDS and returns 501 MethodNotImplemented.
    """
    if not att.local_path:
        log.warning("video attachment %s has no local_path, skipping", att.id)
        return None, f"video attachment {att.id} has no local file"
    try:
        data = Path(att.local_path).read_bytes()
    except OSError as exc:
        log.warning("could not read video file %s: %s", att.local_path, exc)
        return None, _error_detail(exc)

    if len(data) > _BSKY_MAX_VIDEO:
        log.warning("video %s is %d bytes (exceeds %d limit), skipping", att.local_path, len(data), _BSKY_MAX_VIDEO)
        return None, f"video is {len(data)} bytes (limit {_BSKY_MAX_VIDEO})"

    try:
        response = await client.upload_blob(data)
        return response.blob, None
    except Exception as exc:
        log.warning("video upload failed for %s: %s", att.local_path, exc)
        return None, _error_detail(exc)


async def _publish_video(
    client: AsyncClient,
    links: list[SubmissionLink],
    attachments: list[Attachment],
    labels,
    tags: list[str],
    *,
    reply=None,
    source_note: str | None = None,
) -> PublishResult:
    """Publish the first video attachment as the root post."""
    first_video = next((a for a in attachments if a.is_video), None)
    if first_video is None:
        return PublishResult(success=False, error="no video attachment found")

    blob, err = await _upload_video_blob(client, first_video)
    if blob is None:
        detail = f": {err}" if err else ""
        return PublishResult(success=False, error=f"video upload failed{detail}")

    aspect_ratio = None
    if first_video.width and first_video.height:
        aspect_ratio = models.AppBskyEmbedDefs.AspectRatio(
            width=first_video.width, height=first_video.height
        )

    embed = models.AppBskyEmbedVideo.Main(
        video=blob,
        alt=first_video.alt_text_body or "",
        aspect_ratio=aspect_ratio,
    )

    primary = links[0] if links else None
    url = primary.canonical_url if primary else None
    title = primary.resolved_title if primary else None
    if url:
        text, facets = _post_text_and_facets(title, url)
    else:
        # No link: a confirmed non-URL source, or the waived "source unknown" fallback.
        text, facets = _sourceless_text(source_note), []
    text, facets = _append_tags(text, facets, tags)
    return await _create_post(client, text=text, facets=facets, embed=embed, labels=labels, reply=reply)


async def _publish_video_reply(
    client: AsyncClient,
    att: Attachment,
    links: list[SubmissionLink],
    labels,
    tags: list[str],
    root_uri: str,
    root_cid: str,
    parent_uri: str,
    parent_cid: str,
) -> tuple[str, str]:
    """Post a single video attachment as a reply in the thread. Returns (at_uri, cid)."""
    blob, err = await _upload_video_blob(client, att)
    if blob is None:
        detail = f": {err}" if err else ""
        raise RuntimeError(f"video upload failed for attachment {att.id}{detail}")

    aspect_ratio = None
    if att.width and att.height:
        from atproto import models as bsky_models
        aspect_ratio = bsky_models.AppBskyEmbedDefs.AspectRatio(
            width=att.width, height=att.height
        )

    embed = models.AppBskyEmbedVideo.Main(
        video=blob,
        alt=att.alt_text_body or "",
        aspect_ratio=aspect_ratio,
    )
    primary = links[0] if links else None
    url = primary.canonical_url if primary else None
    title = primary.resolved_title if primary else None
    if url:
        text, facets = _post_text_and_facets(title, url)
    else:
        text, facets = "", []
    text, facets = _append_tags(text, facets, tags)

    reply_ref = models.AppBskyFeedPost.ReplyRef(
        root=models.ComAtprotoRepoStrongRef.Main(uri=root_uri, cid=root_cid),
        parent=models.ComAtprotoRepoStrongRef.Main(uri=parent_uri, cid=parent_cid),
    )
    result = await _create_post(client, text=text, facets=facets, embed=embed, labels=labels, reply=reply_ref)
    if not result.at_uri or not result.at_cid:
        raise RuntimeError("video reply post returned no URI/CID")
    await _like_quietly(client, result.at_uri, result.at_cid)
    return result.at_uri, result.at_cid


async def _publish_image_reply(
    client: AsyncClient,
    image_atts: list[Attachment],
    links: list[SubmissionLink],
    labels,
    tags: list[str],
    root_uri: str,
    root_cid: str,
    parent_uri: str,
    parent_cid: str,
) -> tuple[str, str]:
    """Post up to 4 images as a reply in the thread. Returns (at_uri, cid)."""
    images = []
    upload_errors: list[str] = []
    for att in image_atts[:4]:
        if not att.local_path:
            log.warning("image attachment %s has no local_path, skipping", att.id)
            upload_errors.append(f"attachment {att.id} has no local file")
            continue
        blob, err = await _upload_blob(client, att.local_path)
        if blob is None:
            if err:
                upload_errors.append(err)
            continue
        images.append(
            models.AppBskyEmbedImages.Image(
                image=blob,
                alt=att.alt_text_body or "",
            )
        )
    if not images:
        detail = f": {upload_errors[-1]}" if upload_errors else ""
        raise RuntimeError(f"no images could be uploaded for image reply{detail}")

    embed = models.AppBskyEmbedImages.Main(images=images)
    primary = links[0] if links else None
    url = primary.canonical_url if primary else None
    title = primary.resolved_title if primary else None
    if url:
        text, facets = _post_text_and_facets(title, url)
    else:
        text, facets = "", []
    text, facets = _append_tags(text, facets, tags)

    reply_ref = models.AppBskyFeedPost.ReplyRef(
        root=models.ComAtprotoRepoStrongRef.Main(uri=root_uri, cid=root_cid),
        parent=models.ComAtprotoRepoStrongRef.Main(uri=parent_uri, cid=parent_cid),
    )
    result = await _create_post(client, text=text, facets=facets, embed=embed, labels=labels, reply=reply_ref)
    if not result.at_uri or not result.at_cid:
        raise RuntimeError("image reply post returned no URI/CID")
    await _like_quietly(client, result.at_uri, result.at_cid)
    return result.at_uri, result.at_cid


def _append_tags(text: str, facets: list, tags: list[str]) -> tuple[str, list]:
    """Append hashtag facets to post text, dropping any that would exceed the limit."""
    for tag in tags:
        fragment = f" #{tag}"
        if len(text) + len(fragment) > _MAX_GRAPHEMES:
            break
        start_bytes = len(text.encode("utf-8"))
        tag_start = start_bytes + 1  # skip the leading space
        tag_end = tag_start + len(f"#{tag}".encode("utf-8"))
        text += fragment
        facets.append(
            models.AppBskyRichtextFacet.Main(
                features=[models.AppBskyRichtextFacet.Tag(tag=tag)],
                index=models.AppBskyRichtextFacet.ByteSlice(byte_start=tag_start, byte_end=tag_end),
            )
        )
    return text, facets


def at_uri_to_url(at_uri: str, handle: str | None = None) -> str:
    """Convert an AT URI to a bsky.app web URL for display.

    Pass handle to get a handle-based URL instead of a DID-based one.
    Only works for feed post records; other record types return the AT URI as-is.
    """
    parts = at_uri.removeprefix("at://").split("/")
    if len(parts) >= 3 and parts[1] == "app.bsky.feed.post":
        authority = handle or parts[0]
        return f"https://bsky.app/profile/{authority}/post/{parts[2]}"
    return at_uri
