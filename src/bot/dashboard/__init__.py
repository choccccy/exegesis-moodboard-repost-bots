"""Read-only observability dashboard for the moodboard repost bot."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..db import init_engine, session_scope
from ..version import __version__
from . import queries as q
from .settings import DashboardSettings

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@asynccontextmanager
async def _lifespan(app: FastAPI):
    settings = DashboardSettings()  # type: ignore[call-arg]
    app.state.settings = settings
    init_engine(settings.database_url)
    yield


app = FastAPI(lifespan=_lifespan)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    settings: DashboardSettings = request.app.state.settings
    async with session_scope() as session:
        boards = await q.board_stats(session, settings)
        publishes = await q.recent_publishes(session, settings)
        pending = await q.pending_submissions(session)
        errors = await q.recent_errors(session)

    tz = ZoneInfo(settings.queue_timezone)
    loaded_at = datetime.now(tz).strftime("%Y-%m-%d %H:%M MT")
    return _TEMPLATES.TemplateResponse(
        request,
        "index.html",
        {
            "boards": boards,
            "publishes": publishes,
            "pending": pending,
            "errors": errors,
            "loaded_at": loaded_at,
            "version": __version__,
        },
    )


@app.get("/boards/{board_name}", response_class=HTMLResponse)
async def board_queue_view(request: Request, board_name: str):
    settings: DashboardSettings = request.app.state.settings
    async with session_scope() as session:
        board, items = await q.board_queue(session, board_name, settings)
    if board is None:
        return RedirectResponse("/", status_code=302)
    handle = settings.bluesky_handle_for(board_name)
    tz = ZoneInfo(settings.queue_timezone)
    loaded_at = datetime.now(tz).strftime("%Y-%m-%d %H:%M MT")
    return _TEMPLATES.TemplateResponse(
        request,
        "queue.html",
        {
            "board_name": board_name,
            "handle": handle,
            "items": items,
            "fresh_window_hours": settings.queue_fresh_window_hours,
            "loaded_at": loaded_at,
        },
    )
