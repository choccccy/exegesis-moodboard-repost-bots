"""Normalized inbound interaction events.

A curation handler consumes one of these instead of a `discord.Interaction`, so the
core says *who acted and what they targeted* without depending on the platform's
response machinery. A per-surface gateway (Discord: `discord_ingest/gateway.py`)
builds the event from the native interaction and later performs the handler's
`HandlerOutcome` against it.
"""

from __future__ import annotations

from dataclasses import dataclass


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
