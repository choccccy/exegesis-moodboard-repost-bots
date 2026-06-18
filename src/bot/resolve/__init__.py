"""Metadata resolution (STUB — Milestone 4).

The full plan defines a tiered fetch strategy: canonical fetch -> official
oEmbed/API -> fetch-helper mirrors -> limited scraping -> human fallback. None of
that is implemented in Milestone 1. This interface exists so the publisher layer
(M2+) has a stable seam to call into. For now it echoes the canonical URL back.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ResolvedMetadata:
    canonical_url: str
    title: str | None = None
    description: str | None = None


async def resolve(canonical_url: str) -> ResolvedMetadata:
    # M1: no network fetch. Returns the canonical URL unchanged.
    return ResolvedMetadata(canonical_url=canonical_url)
