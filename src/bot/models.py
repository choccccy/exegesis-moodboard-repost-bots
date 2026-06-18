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
    # The channel/thread where the bot posts its procedural request replies.
    channel_id: Mapped[int] = mapped_column(BigInteger)
    author_id: Mapped[int] = mapped_column(BigInteger)
    author_display: Mapped[str] = mapped_column(String(200), default="")

    state: Mapped[str] = mapped_column(String(40), default=SubmissionState.INTENT_SUBMITTED.value)
    graphic_status: Mapped[str] = mapped_column(String(20), default=GraphicStatus.UNKNOWN.value)
    graphic_classification_required: Mapped[bool] = mapped_column(Boolean, default=False)

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


class SubmissionLink(Base):
    __tablename__ = "submission_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"), index=True)
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    raw_url: Mapped[str] = mapped_column(Text)
    canonical_url: Mapped[str] = mapped_column(Text)
    domain_family: Mapped[str] = mapped_column(String(40))

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


class Curator(Base):
    __tablename__ = "curators"

    id: Mapped[int] = mapped_column(primary_key=True)
    board_id: Mapped[int] = mapped_column(ForeignKey("boards.id"), index=True)
    discord_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    discord_role_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    board: Mapped["Board"] = relationship(back_populates="curators")
