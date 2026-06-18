# syntax=docker/dockerfile:1
FROM python:3.12-slim

# uv for fast, reproducible installs
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Project metadata + source are both needed to build/install the wheel.
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN uv pip install --system --no-cache .

# Migrations + alembic config (not part of the installed package).
COPY migrations/ ./migrations/
COPY alembic.ini ./

ENV PYTHONPATH=/app/src

# Apply migrations, then launch the bot. Migrations are idempotent (upgrade head).
ENTRYPOINT ["sh", "-c", "alembic upgrade head && python -m bot.main"]
