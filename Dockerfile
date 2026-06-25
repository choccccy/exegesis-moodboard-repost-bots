# syntax=docker/dockerfile:1
FROM python:3.12-slim

# uv for fast, reproducible installs
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

# Project metadata + source are both needed to build/install the wheel.
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN uv pip install --system --no-cache .

# Migrations + alembic config (not part of the installed package).
COPY migrations/ ./migrations/
COPY alembic.ini ./

ENV PYTHONPATH=/app/src

# Run migrations (idempotent), then exec whatever CMD is passed.
# The "sh" arg sets $0 so $@ correctly captures CMD arguments.
ENTRYPOINT ["sh", "-c", "alembic upgrade head && exec \"$@\"", "sh"]
CMD ["python", "-m", "bot.main"]
