"""Shared curation primitives: submission-lock registry, the DB-scope context managers, and `_now`.

Part of the surface-agnostic curation core (no Discord/chat SDK imports; guarded by
tests/test_curation_boundary.py). Was previously all in curation/core.py.
"""
from __future__ import annotations
import asyncio
import contextlib
import io
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from ..accessibility import initial_alt_text, is_image_attachment, is_video_attachment
from ..asset_store import (
    StorageFullError,
    download_attachment,
    has_free_space,
    remove_submission_dir,
    submission_dir,
)
from ..canonicalize import canonicalize, is_bluesky_post_url
from ..mirrors import mirror_hint_for_url
from ..config import BoardConfig, Settings
from ..models import (
    AttachmentAltTextRequest,
    Attachment,
    Board,
    CancellationRequest,
    ConfirmationRequest,
    ContentLabelRequest,
    ImageRequest,
    MetadataRequest,
    PublishAttempt,
    SourceRequest,
    Submission,
    SubmissionLink,
    SubmissionThread,
    SupplementalImageRequest,
    SupplementalLinkRequest,
    YoutubePlaylistAdd,
)
from .. import publish as publisher
from ..moderation import (
    GRAPHIC_YES_EMOJI,
    graphic_from_emoji,
)
from ..resolve import ResolvedMetadata, resolve, resolve_bluesky_at_uri
from ..state import (
    AltTextStatus,
    GraphicStatus,
    Gap,
    PublishOutcome,
    SubmissionSnapshot,
    SubmissionState,
    evaluate_state,
    missing_gaps,
)
from .surface import NullSurface, Surface, SurfaceError
from .types import InboundAttachment, InboundMessage
from . import prompts, replies
from .events import InteractionEvent, ReactionEvent, ReplyEvent
from .outcomes import Ack, HandlerOutcome, Noop, OpenModal, Tombstone
from .urls import extract_urls, is_discord_internal_url
from .components import PreviewImage
from ..db import session_scope

log = logging.getLogger(__name__)


# Keyed by submission ID. Serializes recompute_and_request per submission so its
# read-decide-send-persist critical section stays atomic once it releases the global
# DB lock around Discord sends (docs/db-lock-io-refactor.md, surface-agnostic #50).
# Replaces the incidental mutual exclusion the global lock used to provide.
_submission_locks: dict[int, asyncio.Lock] = {}


def _submission_lock(submission_id: int) -> asyncio.Lock:
    return _submission_locks.setdefault(submission_id, asyncio.Lock())


@contextlib.asynccontextmanager
async def _maybe_submission_lock(ambient_session, submission_id: int):
    """Take the per-submission lock only for self-managing recomputes.

    Legacy in-session callers (``ambient_session`` set) already hold the global DB
    lock, which serializes them; taking the per-submission lock too would invert the
    lock order versus self-managing callers (which take per-submission then global)
    and could deadlock. So skip it for them.
    """
    if ambient_session is not None:
        yield
    else:
        async with _submission_lock(submission_id):
            yield


@contextlib.asynccontextmanager
async def _scope(ambient_session):
    """A DB scope that reuses the caller's ambient session (legacy in-session path,
    stays inside their transaction/lock) or opens a fresh short session_scope
    (self-managing path, so sends between scopes run with the lock released)."""
    if ambient_session is not None:
        yield ambient_session
    else:
        async with session_scope() as s:
            yield s


def _now() -> datetime:
    return datetime.now(timezone.utc)
