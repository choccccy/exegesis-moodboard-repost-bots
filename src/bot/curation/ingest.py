"""Ingest pipeline: normalize an InboundMessage into links/media, download + resolve, persist.

Part of the surface-agnostic curation core (no Discord/chat SDK imports; guarded by
tests/test_curation_boundary.py). Was previously all in curation/core.py.
"""
from __future__ import annotations
import asyncio
import logging
import os
from dataclasses import dataclass
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ..accessibility import initial_alt_text, is_image_attachment, is_video_attachment
from ..asset_store import StorageFullError, download_attachment, has_free_space, submission_dir
from ..canonicalize import canonicalize
from ..config import Settings
from ..models import Attachment, Submission, SubmissionLink
from ..resolve import ResolvedMetadata, resolve, resolve_bluesky_at_uri
from .types import InboundAttachment, InboundMessage
from .urls import extract_urls, is_discord_internal_url
from ..db import session_scope
from . import base

log = logging.getLogger(__name__)


@dataclass
class _AttachmentPlan:
    """A persisted Attachment skeleton row awaiting its downloaded file."""
    row_id: int
    url: str
    filename: str
    is_video: bool


@dataclass
class _LinkPlan:
    """A persisted SubmissionLink skeleton row awaiting resolution."""
    link_id: int
    canonical_url: str
    domain_family: str
    is_primary: bool


@dataclass
class _IngestPlan:
    """Skeleton rows + context captured under the lock, fed to the (lockless)
    download/resolve gather phase."""
    submission_id: int
    board_id: int
    link_plans: list[_LinkPlan]
    att_plans: list[_AttachmentPlan]
    embed_title: str | None
    embed_description: str | None
    embed_thumb_url: str | None
    thumb_proxy_url: str | None
    # A video attachment already exists (Discord-uploaded here, or a resolver
    # video preserved across reingest) - suppresses re-downloading a resolved one.
    has_existing_video: bool


@dataclass
class _LinkOutcome:
    link_id: int
    title: str | None
    description: str | None
    image_url: str | None
    via: str | None
    source_at_uri: str | None
    image_path: str | None
    video_url: str | None = None
    video_width: int | None = None
    video_height: int | None = None
    video_path: str | None = None


@dataclass
class _IngestOutcome:
    """Results of the gather phase, written back to the rows under the lock."""
    link_outcomes: list[_LinkOutcome]
    att_paths: dict[int, str]


def _extract_raw_urls(message: InboundMessage) -> list[str]:
    """Candidate source URLs from message text, then embed URLs (mobile share
    sheets), then forwarded-snapshot content/embeds - first non-empty wins."""
    # Drop Discord navigation links (jump/message/channel/invite) at every stage:
    # they show up when a message quotes or forwards another Discord message, and
    # are never the actual source. Filtering per-stage (not just at the end) lets a
    # message whose text is ONLY a jump link fall through to the forwarded snapshot
    # that carries the real source URL.
    raw_urls = [u for u in extract_urls(message.content) if not is_discord_internal_url(u)]
    if not raw_urls:
        seen: set[str] = set()
        for embed in message.embeds:
            if embed.url and embed.url not in seen and not is_discord_internal_url(embed.url):
                seen.add(embed.url)
                raw_urls.append(embed.url)
    if not raw_urls:
        for snap in message.snapshots:
            snap_urls = [u for u in extract_urls(snap.content or "") if not is_discord_internal_url(u)]
            if snap_urls:
                raw_urls.extend(snap_urls)
                break
            for embed in snap.embeds:
                if embed.url and not is_discord_internal_url(embed.url):
                    raw_urls.append(embed.url)
                    break
            if raw_urls:
                break
    return raw_urls


async def _persist_ingest_skeletons(
    session: AsyncSession, submission: Submission, message: InboundMessage
) -> _IngestPlan:
    """Create the SubmissionLink + Attachment skeleton rows (no downloads) and
    capture the embed. DB only, so it runs under a short lock; the returned plan
    is what the lockless gather phase downloads/resolves against."""
    for i, raw in enumerate(_extract_raw_urls(message)):
        res = canonicalize(raw)
        session.add(SubmissionLink(
            submission_id=submission.id, order_index=i,
            raw_url=raw, canonical_url=res.canonical_url, domain_family=res.domain_family,
        ))
    await session.flush()
    links = list((await session.scalars(
        select(SubmissionLink)
        .where(SubmissionLink.submission_id == submission.id)
        .order_by(SubmissionLink.order_index)
    )).all())
    link_plans = [
        _LinkPlan(link_id=link.id, canonical_url=link.canonical_url,
                  domain_family=link.domain_family, is_primary=(idx == 0))
        for idx, link in enumerate(links)
    ]

    thumb_proxy_url = _capture_embed(submission, message)

    all_attachments = list(message.attachments)
    for snap in message.snapshots:
        all_attachments.extend(snap.attachments)
    created: list[tuple[Attachment, InboundAttachment, bool]] = []
    for att in all_attachments:
        is_img = is_image_attachment(att.content_type, att.filename)
        is_vid = is_video_attachment(att.content_type, att.filename)
        status, body = initial_alt_text(is_image=is_img, is_video=is_vid, discord_description=att.description)
        row = Attachment(
            submission_id=submission.id, discord_attachment_id=att.id, filename=att.filename,
            discord_url=att.url, mime=att.content_type, width=att.width, height=att.height,
            spoiler=att.spoiler, is_image=is_img, is_video=is_vid,
            alt_text_status=status.value, alt_text_body=body,
        )
        session.add(row)
        created.append((row, att, is_vid))
    await session.flush()
    att_plans = [
        _AttachmentPlan(row_id=row.id, url=att.url, filename=att.filename, is_video=is_vid)
        for row, att, is_vid in created
    ]

    # Covers both a Discord video just created above and a resolver video preserved
    # across reingest (discord_attachment_id == 0), so gather won't re-download one.
    has_existing_video = await session.scalar(
        select(Attachment.id).where(
            Attachment.submission_id == submission.id, Attachment.is_video.is_(True)
        )
    ) is not None

    return _IngestPlan(
        submission_id=submission.id, board_id=submission.board_id,
        link_plans=link_plans, att_plans=att_plans,
        embed_title=submission.embed_title, embed_description=submission.embed_description,
        embed_thumb_url=submission.embed_thumb_url, thumb_proxy_url=thumb_proxy_url,
        has_existing_video=has_existing_video,
    )


async def _download_attachment_file(
    plan: _AttachmentPlan, dest: str, settings: Settings, http_client: httpx.AsyncClient
) -> str | None:
    """Download one attachment's bytes (transcoding video). HTTP only; returns the
    local path, or None on failure/storage-full."""
    try:
        path = await download_attachment(
            url=plan.url, dest_dir=dest, filename=f"{plan.row_id}_{plan.filename}",
            data_dir=settings.data_dir, min_free_mb=settings.storage_min_free_mb, client=http_client,
        )
    except StorageFullError:
        log.warning("storage full: attachment row %s not downloaded", plan.row_id)
        return None
    except (httpx.HTTPError, OSError) as exc:
        log.warning("failed to download attachment row %s: %s", plan.row_id, exc)
        return None
    if plan.is_video and path:
        path = await _transcode_video(path)
    return path


async def _download_thumb(
    plan: _IngestPlan, link: _LinkPlan, image_url: str, dest: str,
    settings: Settings, http_client: httpx.AsyncClient,
) -> str | None:
    """Download a link's resolved thumbnail, falling back to the Discord proxy
    copy for the primary link when the source CDN blocks us. HTTP only."""
    extra_headers: dict[str, str] = {}
    if "upload.wikimedia.org" in image_url:
        from ..resolve.fetch import _UA as _RESOLVE_UA
        extra_headers["Referer"] = "https://en.wikipedia.org/"
        extra_headers["User-Agent"] = _RESOLVE_UA
    try:
        return await download_attachment(
            url=image_url, dest_dir=dest, filename=f"thumb_{link.link_id}",
            data_dir=settings.data_dir, min_free_mb=settings.storage_min_free_mb,
            client=http_client, headers=extra_headers or None,
        )
    except (StorageFullError, httpx.HTTPError, OSError) as exc:
        log.info("thumbnail download failed for link %s: %s", link.link_id, exc)
        # If the original URL failed and we have a Discord proxy copy, try that
        # (sites like FurAffinity whose CDN requires auth/Referer).
        if link.is_primary and plan.thumb_proxy_url and plan.thumb_proxy_url != image_url:
            try:
                path = await download_attachment(
                    url=plan.thumb_proxy_url, dest_dir=dest, filename=f"thumb_{link.link_id}",
                    data_dir=settings.data_dir, min_free_mb=settings.storage_min_free_mb, client=http_client,
                )
                log.info("thumbnail downloaded via Discord proxy for link %s", link.link_id)
                return path
            except (StorageFullError, httpx.HTTPError, OSError) as exc2:
                log.info("Discord proxy thumbnail also failed for link %s: %s", link.link_id, exc2)
        return None


async def _download_resolved_video(
    link: _LinkPlan, meta: ResolvedMetadata, dest: str, settings: Settings, http_client: httpx.AsyncClient
) -> str | None:
    """Download (and transcode) a resolver-provided video for a link. HTTP only;
    returns the local path, or None on failure/oversize so ingest degrades to the
    thumbnail card."""
    filename = f"linkvid_{link.link_id}.mp4"
    if meta.video_is_stream:
        # Stream manifest (e.g. reddit): ffmpeg fetches and muxes video + audio.
        path = await _fetch_stream_video(meta.video_url, dest, filename, settings)
        if path is None:
            log.info("resolved stream video failed for link %s - falling back to thumbnail", link.link_id)
            return None
    else:
        try:
            path = await download_attachment(
                url=meta.video_url, dest_dir=dest, filename=filename,
                data_dir=settings.data_dir, min_free_mb=settings.storage_min_free_mb, client=http_client,
            )
        except (StorageFullError, httpx.HTTPError, OSError) as exc:
            log.info("resolved video download failed for link %s: %s - falling back to thumbnail", link.link_id, exc)
            return None
        path = await _transcode_video(path)
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    if size > _MAX_RESOLVED_VIDEO_BYTES:
        log.info("resolved video for link %s is %d bytes (over %d limit) - falling back to thumbnail",
                 link.link_id, size, _MAX_RESOLVED_VIDEO_BYTES)
        return None
    return path


async def _gather_ingest(
    plan: _IngestPlan, settings: Settings, http_client: httpx.AsyncClient
) -> _IngestOutcome:
    """Download attachments and resolve link metadata/media. **No DB access** - runs
    with the lock released (see docs/db-lock-io-refactor.md)."""
    dest = submission_dir(settings.attachments_dir, plan.board_id, plan.submission_id)

    att_paths: dict[int, str] = {}
    for ap in plan.att_plans:
        path = await _download_attachment_file(ap, dest, settings, http_client)
        if path is not None:
            att_paths[ap.row_id] = path

    link_outcomes: list[_LinkOutcome] = []
    for lp in plan.link_plans:
        meta = await resolve(
            lp.canonical_url, lp.domain_family, client=http_client,
            fallback_title=plan.embed_title if lp.is_primary else None,
            fallback_description=plan.embed_description if lp.is_primary else None,
            fallback_image_url=plan.embed_thumb_url if lp.is_primary else None,
            youtube_api_key=settings.youtube_api_key,
        )
        # Pin the permanent DID now, while the source handle still resolves.
        source_at_uri = (
            await resolve_bluesky_at_uri(lp.canonical_url, http_client)
            if lp.domain_family == "bluesky" else None
        )
        image_path = (
            await _download_thumb(plan, lp, meta.image_url, dest, settings, http_client)
            if meta.image_url else None
        )
        outcome = _LinkOutcome(
            link_id=lp.link_id, title=meta.title, description=meta.description,
            image_url=meta.image_url, via=meta.via, source_at_uri=source_at_uri, image_path=image_path,
        )
        if lp.is_primary and meta.video_url and not plan.has_existing_video:
            video_path = await _download_resolved_video(lp, meta, dest, settings, http_client)
            if video_path is not None:
                outcome.video_url = meta.video_url
                outcome.video_width = meta.video_width
                outcome.video_height = meta.video_height
                outcome.video_path = video_path
        link_outcomes.append(outcome)
    return _IngestOutcome(link_outcomes=link_outcomes, att_paths=att_paths)


async def _attach_resolved_video(
    session: AsyncSession, submission_id: int, link_id: int, *,
    video_url: str | None, video_width: int | None, video_height: int | None, video_path: str,
) -> bool:
    """Create the resolver-sourced video Attachment row, unless the submission
    already has a video (Discord-uploaded or a prior resolve). Returns True if a
    row was created. Shared by ingest and the video backfill admin script."""
    existing = await session.scalar(
        select(Attachment.id).where(
            Attachment.submission_id == submission_id, Attachment.is_video.is_(True)
        )
    )
    if existing is not None:
        return False
    status, body = initial_alt_text(is_image=False, is_video=True, discord_description=None)
    session.add(Attachment(
        submission_id=submission_id, discord_attachment_id=0,
        filename=f"linkvid_{link_id}.mp4", discord_url=video_url, mime="video/mp4",
        width=video_width, height=video_height, is_image=False, is_video=True,
        alt_text_status=status.value, alt_text_body=body,
        local_path=video_path, downloaded_at=base._now(),
    ))
    log.info("resolved video attached for submission %s (link %s)", submission_id, link_id)
    return True


async def _persist_ingest_outcome(
    session: AsyncSession, outcome: _IngestOutcome, submission_id: int
) -> None:
    """Write gathered download paths + resolved metadata back onto the skeleton
    rows, attaching any resolved video. DB only."""
    for row_id, path in outcome.att_paths.items():
        att = await session.get(Attachment, row_id)
        if att is not None:
            att.local_path = path
            att.downloaded_at = base._now()
    for lo in outcome.link_outcomes:
        link = await session.get(SubmissionLink, lo.link_id)
        if link is None:
            continue
        link.resolved_title = lo.title
        link.resolved_description = lo.description
        link.resolved_image_url = lo.image_url
        link.resolved_via = lo.via
        # Cleared for non-bluesky links so a re-resolve after an edit can't leave
        # a stale URI behind.
        link.source_at_uri = lo.source_at_uri
        link.resolved_image_path = lo.image_path
        if lo.video_path is not None:
            await _attach_resolved_video(
                session, submission_id, lo.link_id,
                video_url=lo.video_url, video_width=lo.video_width,
                video_height=lo.video_height, video_path=lo.video_path,
            )


async def ingest_message_content(
    settings: Settings, inbound: InboundMessage, submission_id: int, http_client: httpx.AsyncClient
) -> None:
    """Ingest a submission's links, embed, attachments, and resolved media,
    keeping all HTTP (metadata resolution and thumbnail/attachment/video
    downloads) out of the DB lock (see docs/db-lock-io-refactor.md).

    Self-managing: a short DB scope persists the skeleton rows, a lockless gather
    phase does the downloads/resolution, then a short DB scope writes the results.
    Callers must NOT hold a session_scope. ``inbound`` is the surface-agnostic message
    (the Discord caller converts via ``discord_message_to_inbound``).
    """
    async with session_scope() as session:
        submission = await session.get(Submission, submission_id)
        if submission is None:
            return
        plan = await _persist_ingest_skeletons(session, submission, inbound)
    outcome = await _gather_ingest(plan, settings, http_client)
    async with session_scope() as session:
        await _persist_ingest_outcome(session, outcome, submission_id)


async def _ingest_attachment_in_session(
    session: AsyncSession, submission: Submission, att: InboundAttachment,
    settings: Settings, http_client: httpx.AsyncClient,
) -> None:
    """Create one Attachment row and download its file, in the caller's session.

    Used by the supplemental-content reply handlers, which still hold the DB lock
    during this download; their lockless migration is part of the handler-cascade
    slice (docs/db-lock-io-refactor.md)."""
    is_img = is_image_attachment(att.content_type, att.filename)
    is_vid = is_video_attachment(att.content_type, att.filename)
    status, body = initial_alt_text(is_image=is_img, is_video=is_vid, discord_description=att.description)
    row = Attachment(
        submission_id=submission.id, discord_attachment_id=att.id, filename=att.filename,
        discord_url=att.url, mime=att.content_type, width=att.width, height=att.height,
        spoiler=att.spoiler, is_image=is_img, is_video=is_vid,
        alt_text_status=status.value, alt_text_body=body,
    )
    session.add(row)
    await session.flush()  # assign row.id
    dest = submission_dir(settings.attachments_dir, submission.board_id, submission.id)
    path = await _download_attachment_file(
        _AttachmentPlan(row_id=row.id, url=att.url, filename=att.filename, is_video=is_vid),
        dest, settings, http_client,
    )
    if path is not None:
        row.local_path = path
        row.downloaded_at = base._now()


async def _resolve_links_in_session(
    session: AsyncSession, submission: Submission, settings: Settings, http_client: httpx.AsyncClient,
) -> None:
    """Re-resolve a submission's existing links in the caller's session (still under
    the DB lock). Used by the supplemental-content reply handlers; migrating them to
    the lockless path is part of the handler-cascade slice (docs/db-lock-io-refactor.md)."""
    links = list((await session.scalars(
        select(SubmissionLink)
        .where(SubmissionLink.submission_id == submission.id)
        .order_by(SubmissionLink.order_index)
    )).all())
    if not links:
        return
    has_existing_video = await session.scalar(
        select(Attachment.id).where(
            Attachment.submission_id == submission.id, Attachment.is_video.is_(True)
        )
    ) is not None
    plan = _IngestPlan(
        submission_id=submission.id, board_id=submission.board_id,
        link_plans=[
            _LinkPlan(link_id=link.id, canonical_url=link.canonical_url,
                      domain_family=link.domain_family, is_primary=(idx == 0))
            for idx, link in enumerate(links)
        ],
        att_plans=[], embed_title=submission.embed_title,
        embed_description=submission.embed_description, embed_thumb_url=submission.embed_thumb_url,
        thumb_proxy_url=None, has_existing_video=has_existing_video,
    )
    outcome = await _gather_ingest(plan, settings, http_client)
    await _persist_ingest_outcome(session, outcome, submission.id)


def _capture_embed(submission: Submission, message: InboundMessage) -> str | None:
    """Store the link embed's title/description/thumb on the submission.

    Drives the external-embed preview and the at-least-one-image check. Embeds
    may be absent if ingestion ran before Discord had time to generate them.
    Also checks forwarded-message snapshots.

    Returns the thumbnail proxy_url (if any) as a download fallback for callers.
    """
    all_embeds = list(message.embeds)
    for snap in message.snapshots:
        all_embeds.extend(snap.embeds)
    for embed in all_embeds:
        thumb = embed.thumbnail_url or embed.image_url
        thumb_proxy = embed.thumbnail_proxy_url or embed.image_proxy_url or None
        if embed.title or embed.description or thumb:
            submission.embed_title = embed.title
            submission.embed_description = embed.description
            submission.embed_thumb_url = thumb
            return thumb_proxy
    return None


# Matches the publish-side Bluesky video limit (95 MB headroom under 100 MB).
# Oversize resolved videos are dropped here so the submission falls back to the
# thumbnail card instead of queueing a video that can never upload.
_MAX_RESOLVED_VIDEO_BYTES = 95 * 1024 * 1024


_STREAM_MUX_TIMEOUT = 180  # seconds; ffmpeg fetch+mux of a remote HLS/DASH stream


async def _fetch_stream_video(
    manifest_url: str, dest_dir: str, filename: str, settings: Settings
) -> str | None:
    """Fetch a remote HLS/DASH stream and mux video + audio into an H.264/AAC MP4.

    Used for sources (reddit) that serve video and audio as separate streams: ffmpeg
    reads the manifest, picks the best video+audio, and writes one file. Returns the
    output path, or None on any failure (no space, timeout, ffmpeg error) so ingest
    degrades to the thumbnail card.
    """
    if not has_free_space(settings.data_dir, settings.storage_min_free_mb):
        log.info("no free space for stream video %s", manifest_url)
        return None
    out_path = os.path.join(dest_dir, filename)
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-i", manifest_url,
            "-c:v", "libx264", "-c:a", "aac", "-movflags", "+faststart",
            "-y", out_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        log.warning("could not start ffmpeg for stream %s: %s", manifest_url, exc)
        return None
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_STREAM_MUX_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        log.warning("ffmpeg stream mux timed out for %s", manifest_url)
        return None
    if proc.returncode != 0:
        log.warning("ffmpeg stream mux failed for %s: %s", manifest_url, stderr.decode()[-500:])
        return None
    return out_path


async def _transcode_video(input_path: str) -> str:
    """Transcode a video to H.264 + AAC MP4 suitable for Bluesky upload.

    Returns the path to the transcoded file. Falls back to the original path
    if ffmpeg fails so ingest doesn't hard-crash (publish will fail instead).
    """
    out_path = input_path.rsplit(".", 1)[0] + "_transcoded.mp4"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-i", input_path,
        "-c:v", "libx264", "-c:a", "aac", "-movflags", "+faststart",
        "-y", out_path,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.warning("ffmpeg transcoding failed for %s: %s", input_path, stderr.decode()[-500:])
        return input_path
    try:
        os.remove(input_path)
    except OSError:
        pass
    return out_path
