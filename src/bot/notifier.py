"""Platform-agnostic notification interface used by the ingestion service layer.

`Surface` is the outbound port: a destination (a thread/channel) the curation core
posts to and manages, in surface-agnostic terms (component descriptors from
`bot.components`, not `discord.ui.View`). Concrete implementations live in
platform-specific packages (`discord_ingest.discord_notifier.DiscordSurface`, ...).

`Notifier` is retained as an alias of `Surface` for the pre-existing
`destination: Notifier` call sites during the surface-agnostic migration (issue #50).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from .components import Component, PreviewImage


class SentMessage(Protocol):
    id: int


class Surface(Protocol):
    """A destination (thread/channel) that receives messages and can be managed.

    All outbound curation I/O flows through this so the core never touches Discord
    directly. Send/edit take surface-agnostic component descriptors; the adapter
    renders them. Thread-lifecycle and source-post ops (archive/clear_trigger) live
    here too so handlers stop calling Discord helpers directly.
    """

    async def send(
        self,
        content: str | None = None,
        *,
        components: Sequence[Component] | None = None,
        preview: PreviewImage | None = None,
    ) -> SentMessage: ...

    async def edit(
        self,
        message_id: int,
        *,
        content: str | None = None,
        components: Sequence[Component] | None = None,
    ) -> bool: ...

    async def disable_components(self, message_id: int, label: str) -> bool:
        """Tombstone a message's interactive components, leaving a disabled label."""
        ...

    async def edit_or_none(
        self,
        message_id: int,
        content: str,
        components: Sequence[Component] | None = None,
    ) -> bool:
        """Edit a message in place; return False only if it no longer exists (so the
        caller reposts a fresh one). Transient errors return True (leave it be). The
        live status-checklist upsert path."""
        ...

    async def message_exists(self, message_id: int) -> bool:
        """Whether a previously-sent message still exists. False only on a confirmed
        deletion; assume True if it can't be verified."""
        ...

    async def archive(self, notice: str | None = None) -> None: ...

    def archive_after_delay(self, notice: str | None = None) -> None:
        """Schedule archival after a delay (fire-and-forget, hence sync)."""
        ...

    async def unarchive(self) -> None: ...

    async def clear_trigger(self, source_channel_id: int, source_message_id: int, emoji: str) -> None:
        """Remove the trigger reaction from the original source-channel message."""
        ...


class NullSurface:
    """Drop all outbound operations silently.

    Used by the scheduler when no notification channel is available so that
    publishing still proceeds without a thread to post into. `send` tolerates
    stray legacy kwargs (view=/file=) so it is safe during the migration.
    """

    class _Sent:
        id = 0

    async def send(self, content=None, *, components=None, preview=None, **kwargs) -> SentMessage:
        return self._Sent()

    async def edit(self, message_id, *, content=None, components=None) -> bool:
        return False

    async def disable_components(self, message_id, label) -> bool:
        return False

    async def edit_or_none(self, message_id, content, components=None) -> bool:
        return False

    async def message_exists(self, message_id) -> bool:
        return True

    async def archive(self, notice: str | None = None) -> None:
        pass

    def archive_after_delay(self, notice: str | None = None) -> None:
        pass

    async def unarchive(self) -> None:
        pass

    async def clear_trigger(self, source_channel_id: int, source_message_id: int, emoji: str) -> None:
        pass


# Back-compat aliases for the pre-migration `destination: Notifier` call sites.
Notifier = Surface
NullNotifier = NullSurface
