"""Read-only Bluesky feed generator over already-published moodboard posts.

Serves the three XRPC/well-known endpoints a ``did:web`` feed generator needs. Holds no
credentials: it only reads the bot's database and returns bare post-URI skeletons, which
Bluesky renders itself (applying each post's own content labels). Feed *records* live in
the owner account's repo and are published once by ``bot.admin.publish_feedgen``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from ..db import init_engine, session_scope
from . import queries as q
from .feeds import FEEDS
from .settings import FeedgenSettings


@asynccontextmanager
async def _lifespan(app: FastAPI):
    settings = FeedgenSettings()  # type: ignore[call-arg]
    app.state.settings = settings
    init_engine(settings.database_url)
    yield


app = FastAPI(lifespan=_lifespan)


def _xrpc_error(status: int, error: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": error, "message": message})


@app.get("/.well-known/did.json")
async def did_document(request: Request):
    settings: FeedgenSettings = request.app.state.settings
    return {
        "@context": ["https://www.w3.org/ns/did/v1"],
        "id": settings.service_did,
        "service": [
            {
                "id": "#bsky_fg",
                "type": "BskyFeedGenerator",
                "serviceEndpoint": f"https://{settings.service_hostname}",
            }
        ],
    }


@app.get("/xrpc/app.bsky.feed.describeFeedGenerator")
async def describe_feed_generator(request: Request):
    settings: FeedgenSettings = request.app.state.settings
    return {
        "did": settings.service_did,
        "feeds": [{"uri": settings.feed_uri(rkey)} for rkey in FEEDS],
    }


@app.get("/xrpc/app.bsky.feed.getFeedSkeleton")
async def get_feed_skeleton(
    request: Request,
    feed: str = Query(...),
    limit: int = Query(50),
    cursor: str | None = Query(None),
):
    # The feed AT-URI's rkey selects which feed; ignore any auth JWT (public feed).
    rkey = feed.rsplit("/", 1)[-1]
    feed_def = FEEDS.get(rkey)
    if feed_def is None:
        return _xrpc_error(400, "UnknownFeed", f"unknown feed: {feed}")

    limit = max(1, min(limit, 100))  # clamp to the lexicon's allowed range

    if cursor is not None:
        try:
            q.decode_cursor(cursor)
        except Exception:
            return _xrpc_error(400, "InvalidRequest", "malformed cursor")

    async with session_scope() as session:
        uris, next_cursor = await q.feed_skeleton(
            session, nsfw_allowed=feed_def.nsfw_allowed, limit=limit, cursor=cursor
        )

    body: dict = {"feed": [{"post": uri} for uri in uris]}
    if next_cursor is not None:
        body["cursor"] = next_cursor
    return body
