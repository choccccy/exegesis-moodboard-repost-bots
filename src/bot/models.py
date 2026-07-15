"""SQLAlchemy 2.0 ORM models (Milestone 1 subset of the full schema).

Table names follow the product spec so later milestones extend rather than
rewrite. Discord IDs are 64-bit snowflakes -> BigInteger.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .state import AltTextStatus, GraphicStatus, SubmissionState


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Board(Base):
    __tablename__ = "boards"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    discord_guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    discord_channel_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    nsfw: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    submissions: Mapped[list["Submission"]] = relationship(back_populates="board")
    curators: Mapped[list["Curator"]] = relationship(back_populates="board")


class Submission(Base):
    __tablename__ = "submissions"
    __table_args__ = (
        # A 🦋 reaction on the same message must map to a single submission.
        UniqueConstraint("board_id", "source_discord_message_id", name="uq_submission_source_msg"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    board_id: Mapped[int] = mapped_column(ForeignKey("boards.id"), index=True)
    source_discord_message_id: Mapped[int] = mapped_column(BigInteger, index=True)
    # The parent channel the source message lives in (used for board resolution).
    channel_id: Mapped[int] = mapped_column(BigInteger)
    # The per-submission Discord thread where the bot runs the procedural Q&A.
    thread_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    author_id: Mapped[int] = mapped_column(BigInteger)
    author_display: Mapped[str] = mapped_column(String(200), default="")

    state: Mapped[str] = mapped_column(String(40), default=SubmissionState.INTENT_SUBMITTED.value)
    graphic_status: Mapped[str] = mapped_column(String(20), default=GraphicStatus.UNKNOWN.value)
    graphic_classification_required: Mapped[bool] = mapped_column(Boolean, default=False)

    # Playlist opt-out: set True when ⏹️ is reacted on the opt-out prompt.
    playlist_skipped: Mapped[bool] = mapped_column(Boolean, default=False)
    # Message ID of the ⏹️ opt-out prompt posted in the submission thread.
    playlist_opt_out_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # Set True when an OP/curator marks the post as having no findable source; the
    # SOURCE requirement is then waived and the post publishes with a "source unknown" note.
    source_waived: Mapped[bool] = mapped_column(Boolean, default=False)
    # Free-text, non-URL source (e.g. "Popular Mechanics, March 1965") for images with no
    # canonical link. Held unconfirmed until the replier OKs the "that's not a URL" prompt;
    # only counts as a source (closes the SOURCE gap, publishes as "source: ...") once
    # source_note_confirmed is True.
    source_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_note_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    # Message ID of the live "post status" checklist message in the submission thread,
    # edited in place as gaps are filled. Null until the checklist is first posted.
    status_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # Captured from the Discord-generated link embed at ingest. Feeds the
    # external-embed preview and the at-least-one-image check (thumb).
    embed_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    embed_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    embed_thumb_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # When the original Discord message was posted (used for freshness / queue ordering).
    # Populated from message.created_at at ingest; kept distinct from created_at (ingest time).
    source_posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Discord snowflake of the message this submission is a reply to, if any.
    # Used to form Bluesky reply chains when both messages are butterflied.
    reply_to_discord_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    board: Mapped["Board"] = relationship(back_populates="submissions")
    links: Mapped[list["SubmissionLink"]] = relationship(
        back_populates="submission", cascade="all, delete-orphan", order_by="SubmissionLink.order_index"
    )
    attachments: Mapped[list["Attachment"]] = relationship(
        back_populates="submission", cascade="all, delete-orphan"
    )
    source_requests: Mapped[list["SourceRequest"]] = relationship(
        back_populates="submission", cascade="all, delete-orphan"
    )
    alt_text_requests: Mapped[list["AttachmentAltTextRequest"]] = relationship(
        back_populates="submission", cascade="all, delete-orphan"
    )
    content_label_requests: Mapped[list["ContentLabelRequest"]] = relationship(
        back_populates="submission", cascade="all, delete-orphan"
    )
    image_requests: Mapped[list["ImageRequest"]] = relationship(
        back_populates="submission", cascade="all, delete-orphan"
    )
    metadata_requests: Mapped[list["MetadataRequest"]] = relationship(
        back_populates="submission", cascade="all, delete-orphan"
    )
    supplemental_image_requests: Mapped[list["SupplementalImageRequest"]] = relationship(
        back_populates="submission", cascade="all, delete-orphan"
    )
    supplemental_link_requests: Mapped[list["SupplementalLinkRequest"]] = relationship(
        back_populates="submission", cascade="all, delete-orphan"
    )
    publish_attempts: Mapped[list["PublishAttempt"]] = relationship(
        back_populates="submission", cascade="all, delete-orphan"
    )


class SubmissionLink(Base):
    __tablename__ = "submission_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"), index=True)
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    raw_url: Mapped[str] = mapped_column(Text)
    canonical_url: Mapped[str] = mapped_column(Text)
    domain_family: Mapped[str] = mapped_column(String(40))

    # Metadata resolved from the source (oembed / opengraph / html / discord / none).
    resolved_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Local path of the downloaded thumbnail bytes (the future Bluesky blob).
    resolved_image_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_via: Mapped[str | None] = mapped_column(String(20), nullable=True)

    submission: Mapped["Submission"] = relationship(back_populates="links")


class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"), index=True)
    discord_attachment_id: Mapped[int] = mapped_column(BigInteger, index=True)
    filename: Mapped[str] = mapped_column(String(500))
    local_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    discord_url: Mapped[str] = mapped_column(Text)
    mime: Mapped[str | None] = mapped_column(String(100), nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    spoiler: Mapped[bool] = mapped_column(Boolean, default=False)
    is_image: Mapped[bool] = mapped_column(Boolean, default=True)
    is_video: Mapped[bool] = mapped_column(Boolean, default=False)

    alt_text_status: Mapped[str] = mapped_column(String(20), default=AltTextStatus.NEEDED.value)
    alt_text_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    alt_text_author: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    submission: Mapped["Submission"] = relationship(back_populates="attachments")


class _RequestMixin:
    """Shared columns for the bot-prompt/human-reply tracking tables."""

    id: Mapped[int] = mapped_column(primary_key=True)
    # The bot's request message ID; replies are matched against this.
    bot_message_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    prompted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    answered_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class SourceRequest(_RequestMixin, Base):
    __tablename__ = "source_requests"

    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"), index=True)
    submission: Mapped["Submission"] = relationship(back_populates="source_requests")


class AttachmentAltTextRequest(_RequestMixin, Base):
    __tablename__ = "attachment_alt_text_requests"

    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"), index=True)
    attachment_id: Mapped[int] = mapped_column(ForeignKey("attachments.id"), index=True)
    submission: Mapped["Submission"] = relationship(back_populates="alt_text_requests")


class ContentLabelRequest(_RequestMixin, Base):
    __tablename__ = "content_label_requests"

    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"), index=True)
    submission: Mapped["Submission"] = relationship(back_populates="content_label_requests")


class ImageRequest(_RequestMixin, Base):
    __tablename__ = "image_requests"

    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"), index=True)
    submission: Mapped["Submission"] = relationship(back_populates="image_requests")


class MetadataRequest(_RequestMixin, Base):
    """Tracks a bot prompt asking for a more embeddable link (or 🔗 confirmation).

    answer == "confirmed" means the curator reacted 🔗 (best link as-is).
    answer == <url> means they replied with a replacement link.
    """

    __tablename__ = "metadata_requests"

    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"), index=True)
    submission: Mapped["Submission"] = relationship(back_populates="metadata_requests")


class SupplementalImageRequest(_RequestMixin, Base):
    """Standing offer for OP or curator to attach additional images to a submission.

    Unlike ImageRequest (which blocks on a missing-image gap), this is always available
    and re-posted after each batch of images is received.
    """

    __tablename__ = "supplemental_image_requests"

    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"), index=True)
    submission: Mapped["Submission"] = relationship(back_populates="supplemental_image_requests")


class SupplementalLinkRequest(_RequestMixin, Base):
    """Standing offer for OP or curator to add extra links as thread-reply posts.

    Unlike SourceRequest (which blocks on a missing-link gap), this is always
    available once a source link exists and re-posted after each batch is received.
    """

    __tablename__ = "supplemental_link_requests"

    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"), index=True)
    submission: Mapped["Submission"] = relationship(back_populates="supplemental_link_requests")


class PublishAttempt(Base):
    """Audit log of every Bluesky post attempt for a submission."""

    __tablename__ = "publish_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"), index=True)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    success: Mapped[bool] = mapped_column(Boolean)
    # AT URI and CID returned by the Bluesky API on success.
    at_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    at_cid: Mapped[str | None] = mapped_column(Text, nullable=True)
    # For reply chains: the root post's AT URI and CID. Equal to at_uri/at_cid for top-level posts.
    bsky_root_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    bsky_root_cid: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Human-readable bsky.app URL for the published content (handle-based, not DID).
    bsky_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    submission: Mapped["Submission"] = relationship(back_populates="publish_attempts")


class SubmissionThread(Base):
    """Durable map from a source message to its private Discord thread.

    Outlives the Submission (not cascade-deleted) so that removing + re-adding the
    🦋 reuses the same private thread instead of spawning a new one and re-pinging
    curators.
    """

    __tablename__ = "submission_threads"
    __table_args__ = (
        UniqueConstraint(
            "board_id", "source_discord_message_id", name="uq_submission_thread_source"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    board_id: Mapped[int] = mapped_column(ForeignKey("boards.id"), index=True)
    source_discord_message_id: Mapped[int] = mapped_column(BigInteger, index=True)
    thread_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Curator(Base):
    __tablename__ = "curators"

    id: Mapped[int] = mapped_column(primary_key=True)
    board_id: Mapped[int] = mapped_column(ForeignKey("boards.id"), index=True)
    discord_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    discord_role_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    board: Mapped["Board"] = relationship(back_populates="curators")


class CancellationRequest(Base):
    """Tracks the per-thread ❌ cancel button message.

    One row per submission; the bot message is pre-seeded with ❌. If the OP or
    a curator reacts ❌, the submission is deleted (same effect as removing 🦋).
    """

    __tablename__ = "cancellation_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"), unique=True, index=True)
    bot_message_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    prompted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ConfirmationRequest(Base):
    """Per-thread ✅ confirmation prompt before queuing.

    One row per submission. When OP or a curator reacts ✅, the submission
    moves to QUEUED. Not cascade-deleted via ORM (manually cleaned up on cancel).
    """

    __tablename__ = "confirmation_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"), unique=True, index=True)
    bot_message_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    prompted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class YoutubePlaylistAdd(Base):
    """Audit log of ▶️ playlist additions; also serves as the dedup table."""

    __tablename__ = "youtube_playlist_add"

    id: Mapped[int] = mapped_column(primary_key=True)
    board_id: Mapped[int] = mapped_column(ForeignKey("boards.id"), index=True)
    source_discord_message_id: Mapped[int] = mapped_column(BigInteger, index=True)
    video_id: Mapped[str] = mapped_column(String(20), index=True)
    playlist_id: Mapped[str] = mapped_column(String(100))
    discord_requester_id: Mapped[int] = mapped_column(BigInteger)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    success: Mapped[bool] = mapped_column(Boolean)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    playlist_item_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    cancel_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)


class BotError(Base):
    """Persistent log of unhandled exceptions in background tasks."""

    __tablename__ = "bot_errors"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(index=True)   # e.g. "scheduler"
    context: Mapped[str]                               # e.g. "board robot-posting"
    traceback: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
