"""Persistent error recording for background tasks."""

from __future__ import annotations

import logging
import traceback as tb

from .db import session_scope
from .models import BotError

log = logging.getLogger(__name__)


async def record_error(source: str, context: str) -> None:
    """Write the current exception to the bot_errors table.

    Must be called inside an except block so traceback.format_exc() is populated.
    Swallows any DB failure so a recording error never masks the original one.
    """
    trace = tb.format_exc()
    try:
        async with session_scope() as session:
            session.add(BotError(source=source, context=context, traceback=trace))
    except Exception:
        log.exception("failed to record error (source=%s context=%s)", source, context)
