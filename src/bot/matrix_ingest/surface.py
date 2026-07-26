"""Skeleton Matrix implementation of the surface-agnostic `Surface` port (issue #50).

This is a structural placeholder, deliberately not wired to a Matrix homeserver (we
don't run one yet). It exists to prove the ports/adapters seam end-to-end: the curation
core in `bot.curation` talks only to the `Surface` port, so a second surface drops in
here without touching the core. Each method raises `NotImplementedError`; when a
homeserver is available, fill them in against a Matrix client (e.g. matrix-nio) - the
core stays unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence

from ..curation.components import Component, PreviewImage
from ..curation.surface import SentMessage

_TODO = "MatrixSurface is a skeleton - no Matrix homeserver is wired up yet"


class MatrixSurface:
    """`Surface` adapter for Matrix. Skeleton: methods raise until a client is wired.

    Structurally conforms to `bot.curation.surface.Surface` (verified in tests), so the
    curation handlers could run against it verbatim once the bodies are implemented.
    """

    async def send(
        self,
        content: str | None = None,
        *,
        components: Sequence[Component] | None = None,
        preview: PreviewImage | None = None,
    ) -> SentMessage:
        raise NotImplementedError(_TODO)

    async def edit(
        self,
        message_id: int,
        *,
        content: str | None = None,
        components: Sequence[Component] | None = None,
    ) -> bool:
        raise NotImplementedError(_TODO)

    async def disable_components(self, message_id: int, label: str) -> bool:
        raise NotImplementedError(_TODO)

    async def edit_or_none(
        self,
        message_id: int,
        content: str,
        components: Sequence[Component] | None = None,
    ) -> bool:
        raise NotImplementedError(_TODO)

    async def message_exists(self, message_id: int) -> bool:
        raise NotImplementedError(_TODO)

    async def archive(self, notice: str | None = None) -> None:
        raise NotImplementedError(_TODO)

    def archive_after_delay(self, notice: str | None = None, *, delay: float | None = None) -> None:
        raise NotImplementedError(_TODO)

    async def unarchive(self) -> None:
        raise NotImplementedError(_TODO)

    async def clear_trigger(self, source_channel_id: int, source_message_id: int, emoji: str) -> None:
        raise NotImplementedError(_TODO)
