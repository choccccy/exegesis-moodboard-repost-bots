"""Handler outcomes: what a curation handler wants done in reply to an interaction.

A handler returns one of these instead of calling `interaction.response.*` directly.
A per-surface gateway performs it (Discord: `discord_ingest/gateway.py`), so the core
never touches the platform's response API. The set grows as more handlers migrate
(later: tombstoning the clicked message, explicit defer); today's three modal/picker
openers need only these.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ..components import Component, ModalSpec


@dataclass(frozen=True)
class Ack:
    """Reply to the actor (ephemeral by default), optionally carrying components (e.g.
    a select picker)."""

    message: str
    ephemeral: bool = True
    components: Sequence[Component] | None = None


@dataclass(frozen=True)
class OpenModal:
    """Pop a form open in front of the actor."""

    spec: ModalSpec


@dataclass(frozen=True)
class Tombstone:
    """Disable the clicked message's controls in place, replacing them with a single
    greyed-out button carrying `label` (e.g. "Queued ✅"). The handler has already done
    its thread-side work through the Surface; this is just the reply to the click."""

    label: str


@dataclass(frozen=True)
class Noop:
    """Do nothing (e.g. an empty select submission)."""


HandlerOutcome = Ack | OpenModal | Tombstone | Noop
