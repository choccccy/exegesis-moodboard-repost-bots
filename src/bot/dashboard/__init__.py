"""Read-only observability dashboard for the moodboard repost bot."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..db import init_engine, session_scope
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
        errors = await q.recent_errors(session)

    loaded_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return _TEMPLATES.TemplateResponse(
        request,
        "index.html",
        {
            "boards": boards,
            "publishes": publishes,
            "errors": errors,
            "loaded_at": loaded_at,
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
    loaded_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
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
