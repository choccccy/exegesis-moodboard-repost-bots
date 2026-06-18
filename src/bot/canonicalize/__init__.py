"""URL canonicalization registry.

This is product, not glue: every repost must expose the *canonical* source URL,
never a mirror or a tracker-laden share link. `canonicalize()` returns the
canonical form plus a coarse `domain_family` used downstream for platform-specific
handling.
"""

from .registry import CanonResult, canonicalize

__all__ = ["CanonResult", "canonicalize"]
