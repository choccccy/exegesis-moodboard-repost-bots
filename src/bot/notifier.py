"""Platform-agnostic notification interface used by the ingestion service layer.

Concrete implementations live in platform-specific packages (discord_ingest, etc.).
"""

from __future__ import annotations

from typing import Protocol


class SentMessage(Protocol):
    id: int


class Notifier(Protocol):
    """A destination that can receive text messages and be archived/closed."""

    async def send(self, content: str | None = None, **kwargs) -> SentMessage: ...

    async def archive(self, notice: str) -> None: ...


class NullNotifier:
    """Drop all outbound messages silently.

    Used by the scheduler when no notification channel is available so that
    publishing still proceeds without a Discord thread to post into.
    """

    class _Sent:
        id = 0

    async def send(self, content: str | None = None, **kwargs) -> SentMessage:
        return self._Sent()

    async def archive(self, notice: str) -> None:
        pass
