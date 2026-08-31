"""Read-only DB queries for the feed generator.

The feed is a thin query over already-published posts: every successful
``PublishAttempt`` is a candidate. Two wrinkles handled here:

- **Reposts.** For a native repost, ``PublishAttempt.at_uri`` is an
  ``app.bsky.feed.repost`` *record* URI (in the bot's repo), which is NOT a valid feed
  ``post`` URI. The original post's URI is ``SubmissionLink.source_at_uri`` (the
  ``order_index == 0`` bluesky link). We substitute it, and drop reposts where it is null
  (legacy rows captured before DID pinning - recoverable via
  ``bot.admin.backfill_repost_source_uris``). All of this is expressed in SQL so every
  returned row already has a valid post URI.
- **Pagination.** Standard keyset pagination on ``(attempted_at, at_uri)`` descending.
  Fair-interleave (below) reorders only the returned page for display; the cursor is the
  time-floor of the page, so paging never skips or duplicates.
"""

from __future__ import annotations

import base64
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import and_, case, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Board, PublishAttempt, Submission, SubmissionLink

_REPOST_MARKER = "app.bsky.feed.repost"


def _to_naive_utc(dt: datetime) -> datetime:
    """SQLite stores datetimes as naive UTC; normalize for DB-level comparisons."""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def encode_cursor(attempted_at: datetime, at_uri: str) -> str:
    raw = f"{_to_naive_utc(attempted_at).isoformat()}::{at_uri}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    """Decode a cursor to (naive-UTC attempted_at, at_uri). Raises ValueError if malformed."""
    raw = base64.urlsafe_b64decode(cursor.encode()).decode()
    ts_str, at_uri = raw.split("::", 1)
    return datetime.fromisoformat(ts_str), at_uri


def interleave_by_board(items: list[tuple[str, str]]) -> list[str]:
    """Fairly interleave time-ordered ``(post_uri, board_name)`` items across boards.

    Each item is ranked by recency *within its board* (0 = newest for that board); the
    page is then ordered by that rank, ties broken by original time position. Result: a
    round-robin across boards by recency, so no single busy board floods a stretch while
    staying roughly time-descending. Pure and deterministic.
    """
    rank: dict[str, int] = defaultdict(int)
    annotated: list[tuple[int, int, str]] = []
    for idx, (uri, board) in enumerate(items):
        annotated.append((rank[board], idx, uri))
        rank[board] += 1
    annotated.sort(key=lambda t: (t[0], t[1]))
    return [uri for _, _, uri in annotated]


async def feed_skeleton(
    session: AsyncSession,
    *,
    nsfw_allowed: bool,
    limit: int,
    cursor: str | None = None,
) -> tuple[list[str], str | None]:
    """Return ``(post_uris, next_cursor)`` for a feed page.

    ``post_uris`` are fair-interleaved for display; ``next_cursor`` is None on the last
    page. Every returned URI is a real ``app.bsky.feed.post`` URI.
    """
    # The post URI to emit: the original post for reposts, else the attempt's own URI.
    post_uri = case(
        (PublishAttempt.at_uri.like(f"%{_REPOST_MARKER}%"), SubmissionLink.source_at_uri),
        else_=PublishAttempt.at_uri,
    )

    stmt = (
        select(post_uri.label("post_uri"), PublishAttempt.attempted_at,
               PublishAttempt.at_uri, Board.name.label("board_name"))
        .join(Submission, PublishAttempt.submission_id == Submission.id)
        .join(Board, Board.id == Submission.board_id)
        .outerjoin(
            SubmissionLink,
            and_(
                SubmissionLink.submission_id == Submission.id,
                SubmissionLink.order_index == 0,
            ),
        )
        .where(
            PublishAttempt.success.is_(True),
            PublishAttempt.error.is_(None),
            PublishAttempt.at_uri.is_not(None),
            post_uri.is_not(None),  # drops null-source legacy reposts
        )
    )
    if not nsfw_allowed:
        stmt = stmt.where(Board.nsfw.is_(False))
    if cursor is not None:
        ts, uri = decode_cursor(cursor)
        stmt = stmt.where(
            or_(
                PublishAttempt.attempted_at < ts,
                and_(PublishAttempt.attempted_at == ts, PublishAttempt.at_uri < uri),
            )
        )

    stmt = stmt.order_by(
        PublishAttempt.attempted_at.desc(), PublishAttempt.at_uri.desc()
    ).limit(limit + 1)  # one extra row tells us whether another page follows

    rows = (await session.execute(stmt)).all()
    page = rows[:limit]
    has_more = len(rows) > limit
    next_cursor = (
        encode_cursor(page[-1].attempted_at, page[-1].at_uri)
        if has_more and page
        else None
    )
    uris = interleave_by_board([(r.post_uri, r.board_name) for r in page])
    return uris, next_cursor
