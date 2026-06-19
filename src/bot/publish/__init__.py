"""Bluesky publisher - Milestones 2 & 3.

M2: external-link card posts and image posts.
M3: native Bluesky reposts and multi-link reply threads.
"""

from __future__ import annotations

import datetime
import logging
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

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
    bsky_url: str | None = None
    is_repost: bool = False


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
    all_tags = list(board_cfg.tags)
    if board_cfg.nsfw:
        all_tags.append("nsfw")

    try:
        if kind == "record":
            result = await _publish_record(client, links)
        elif kind == "external":
            result = await _publish_external(client, links, labels, all_tags)
        elif kind == "images":
            result = await _publish_images(client, links, attachments, labels, all_tags)
        else:
            return PublishResult(success=False, error=f"unsupported embed kind: {kind}")
    except Exception as exc:
        log.error("publish failed for submission %s: %s", submission.id, exc)
        return PublishResult(success=False, error=str(exc))

    if result.success and result.at_uri:
        if kind == "record":
            # For native reposts, show the original post's URL, not the repost record.
            result.bsky_url = links[0].canonical_url
        else:
            try:
                await client.like(result.at_uri, result.at_cid)
            except Exception as exc:
                log.warning("failed to like own post for submission %s: %s", submission.id, exc)
            result.bsky_url = at_uri_to_url(result.at_uri, board_cfg.bluesky_handle)
            if len(links) > 1:
                await _publish_reply_thread(
                    client, links[1:], labels, all_tags,
                    result.at_uri, result.at_cid, submission.id,
                )

    return result


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


async def _resolve_bluesky_post(client: AsyncClient, canonical_url: str) -> tuple[str, str]:
    """Resolve a bsky.app URL to (at_uri, cid).

    Handles both handle-based and DID-based profile URLs.
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
    posts_resp = await client.get_posts([at_uri])
    if not posts_resp.posts:
        raise ValueError(f"Bluesky post not found: {at_uri}")
    return at_uri, posts_resp.posts[0].cid


async def _publish_record(client: AsyncClient, links: list[SubmissionLink]) -> PublishResult:
    """Create a native Bluesky repost of a bsky.app source link."""
    canonical_url = links[0].canonical_url
    try:
        at_uri, cid = await _resolve_bluesky_post(client, canonical_url)
    except Exception as exc:
        return PublishResult(success=False, error=f"could not resolve Bluesky post: {exc}")

    response = await client.repost(at_uri, cid)
    return PublishResult(success=True, at_uri=response.uri, at_cid=response.cid, is_repost=True)


async def _publish_reply_thread(
    client: AsyncClient,
    extra_links: list[SubmissionLink],
    labels,
    tags: list[str],
    root_uri: str,
    root_cid: str,
    submission_id: int,
) -> None:
    """Publish additional links as reply posts chained off the root post."""
    parent_uri = root_uri
    parent_cid = root_cid
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

    reply_ref = models.AppBskyFeedPost.ReplyRef(
        root=models.ComAtprotoRepoStrongRef.Main(uri=root_uri, cid=root_cid),
        parent=models.ComAtprotoRepoStrongRef.Main(uri=parent_uri, cid=parent_cid),
    )
    result = await _create_post(
        client,
        text=text,
        facets=facets,
        embed=None,
        labels=labels,
        reply=reply_ref,
    )
    if not result.at_uri or not result.at_cid:
        raise RuntimeError("reply post creation returned no URI/CID")
    return result.at_uri, result.at_cid


async def _publish_external(
    client: AsyncClient,
    links: list[SubmissionLink],
    labels,
    tags: list[str],
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
    text, facets = _append_tags(text, facets, tags)
    return await _create_post(client, text=text, facets=facets, embed=embed, labels=labels)


async def _publish_images(
    client: AsyncClient,
    links: list[SubmissionLink],
    attachments: list[Attachment],
    labels,
    tags: list[str],
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

    text, facets = _append_tags(text, facets, tags)
    return await _create_post(client, text=text, facets=facets, embed=embed, labels=labels)


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
