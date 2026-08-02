"""Normalized inbound interaction events.

A curation handler consumes one of these instead of a `discord.Interaction`, so the
core says *who acted and what they targeted* without depending on the platform's
response machinery. A per-surface gateway (Discord: `discord_ingest/gateway.py`)
builds the event from the native interaction and later performs the handler's
`HandlerOutcome` against it.
"""

from __future__ import annotations

from dataclasses import dataclass

from .types import InboundMessage


@dataclass(frozen=True)
class InteractionEvent:
    """A button click or select submission, normalized.

    `member` is the platform actor object used for role-based authorization (a Discord
    `Member` today). It is carried opaquely so this module stays surface-agnostic;
    decoupling authorization itself is a later phase. `values` holds a select's chosen
    option values (empty for a plain button).
    """

    user_id: int
    submission_id: int
    member: object | None = None
    values: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReactionEvent:
    """An emoji reaction on a message, normalized.

    `message_id` is the reacted message - a bot request message (label/metadata/confirm/
    cancel prompts) or the original source post (🦋 remove, source ❌). `channel_id` is the
    reacted channel; it is only needed by the react-on-source handlers to find the board.
    `member` is carried opaquely for role auth, exactly like `InteractionEvent.member`.
    """

    user_id: int
    message_id: int
    channel_id: int
    emoji: str
    member: object | None = None


@dataclass(frozen=True)
class ReplyEvent:
    """A human reply to one of the bot's request messages, normalized.

    `bot_message_id` is the request message being answered (the reply's reference target).
    `author_id`/`member` identify the replier for authorization (member carried opaquely).
    `message` is the reply's content/attachments as the surface-agnostic `InboundMessage`.
    """

    bot_message_id: int
    author_id: int
    message: InboundMessage
    member: object | None = None
