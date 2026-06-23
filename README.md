- https://bsky.app/profile/robots.exegesis.space
- https://bsky.app/profile/xxx-robots.exegesis.space
- https://bsky.app/profile/vehicles.exegesis.space
- https://bsky.app/profile/doohickeys.exegesis.space
- https://bsky.app/profile/tv.exegesis.space

# Discord → Bluesky Repost Bot

A source-first bot that turns curated Discord moodboard posts into Bluesky reposts
while always preserving the canonical source URL and image alt text.

## How it works

1. Someone posts a link and/or image attachments in a watched channel.
2. Someone reacts with 🦋 → the bot creates a `submission` and opens a **private thread**
   off that message where all the procedural Q&A happens (keeps the main channel clean).
3. The bot parses + canonicalizes URLs (strips trackers, normalizes known mirrors back to
   their canonical domains), downloads attachments to `/data/attachments/...`, and reuses
   Discord alt text when present.
4. For anything missing, the bot posts one request per gap in the thread:
   - `reply with the source URL` — when no link was found in the original message
   - `couldn't get metadata from X — reply with a more embeddable link, or react 🔗 to use as-is`
   - `this link has no preview image — reply attaching an image to use`
   - `reply with the alt text for <file>`
   - `react 🩸 / ✅ to classify graphic content`
5. The original poster **or any curator-role user** replies/reacts; the bot records the answer
   and re-evaluates. When all requirements are met it posts a verification preview of the
   prospective Bluesky post, then queues it for scheduled posting.
6. Posts fire hourly from noon Mountain Time — up to 6/day when fresh content exists,
   3/day when working through backlog. Freshness is based on the original Discord post time.
7. For Bluesky posts the bot reposts natively; for everything else it creates an external-link
   card or image post.
8. Removing the 🦋 deletes the submission and its thread. Published posts cannot be un-reacted
   (the Bluesky post is already live — contact an admin to take it down).
9. If publish fails, the bot retries automatically at the next queue slot.

---

## Requirements

- [Docker](https://docs.docker.com/get-docker/) + Docker Compose V2
- [1Password CLI](https://developer.1password.com/docs/cli/) (`op`) — for secret injection
- Python 3.12 + [`uv`](https://docs.astral.sh/uv/) — for local dev and running migrations

---

## First-time setup

### 1. Discord bot

1. Go to <https://discord.com/developers/applications> → New Application.
2. Under **Bot → Privileged Gateway Intents**, enable **Message Content Intent** and
   **Server Members Intent**.
3. Invite the bot to your server with scope `bot` and these permissions:
   View Channels, Read Message History, Add Reactions, Send Messages,
   Send Messages in Threads, Attach Files, **Create Public Threads**, **Manage Threads**.
4. Copy the bot token. Store it in 1Password (e.g. item name `exegesis bot`, field `token`).

### 2. Bluesky app passwords

For each board account, go to <https://bsky.app/settings/app-passwords>, create an app
password, and store it in 1Password. The item name is your choice — you'll reference it in
`op.env`.

### 3. Configure `op.env`

`op.env` is the source of truth for all config and is safe to commit — it contains
`op://` references, not real secrets. Secrets are resolved at runtime by `op run`;
they are never written to disk.

**Secret fields** use 1Password URI syntax:
```
op://VAULT/ITEM/FIELD
```
For example, `op://Private/exegesis bot/token` resolves the `token` field from the
`exegesis bot` item in the `Private` vault.

**Non-secret fields** (board JSON, queue settings, etc.) are plain text edited inline.

Key fields to fill in:

| Field | What to put |
|---|---|
| `DISCORD_BOT_TOKEN` | `op://` reference to the bot token |
| `BOARDS_JSON` | JSON array — see format below |
| `BSKY_APP_PASSWORD_<NAME>` | `op://` reference per board (one line each) |

**`BOARDS_JSON` format** (one object per board, kept on one line for env compatibility):
```json
[
  {
    "name": "my-board",
    "discord_guild_id": 123456789,
    "discord_channel_id": 987654321,
    "nsfw": false,
    "curator_role_ids": [111222333],
    "bluesky_handle": "my-board.bsky.social",
    "tags": ["my-board"]
  }
]
```

**`BSKY_APP_PASSWORD_<NAME>`** — board name uppercased with hyphens replaced by
underscores. Board `my-board` → `BSKY_APP_PASSWORD_MY_BOARD`. Also add the bare key
to the `bot` service's `environment:` list in `docker-compose.yml`.

### 4. Adding a new board

1. Add a new object to `BOARDS_JSON` in `op.env`.
2. Add `BSKY_APP_PASSWORD_<BOARD_NAME_UPPER>="op://..."` to `op.env`.
3. Add `- BSKY_APP_PASSWORD_<BOARD_NAME_UPPER>` to the `bot` service environment list in `docker-compose.yml`.
4. Create the Bluesky app password and store it in 1Password.
5. Redeploy (see below).

### 5. DNS (one-time, for the dashboard)

In Porkbun, add an `A` record:
- **Name:** `dashboard`
- **Value:** the droplet's IP address

Caddy will provision a TLS certificate automatically on the first request.

---

## Deploy

The deploy target is the `DigitalOcean-remote` Docker context, which connects to the
droplet over SSH. All `docker compose` commands run remotely — the build happens on the
droplet using the local source tree.

Secrets live in `op.env` as `op://` references and are resolved at runtime by the
1Password CLI — nothing is ever written to disk.

```bash
# Deploy (builds images on the remote host, starts all three services)
op run --env-file op.env --no-masking -- docker --context DigitalOcean-remote compose up --build -d
```

`op run` injects resolved secrets into the subprocess environment. Docker Compose
inherits them and passes them through to each container via the bare-key `environment:`
entries in `docker-compose.yml`.

This starts:
- **`bot`** — the Discord ingestion + Bluesky publish bot
- **`dashboard`** — read-only web dashboard at https://dashboard.exegesis.space
- **`caddy`** — reverse proxy; handles TLS automatically via Let's Encrypt

Data (SQLite DB, downloaded attachments, logs) lives in the `bot-data` named volume and
survives rebuilds. Caddy's TLS certificates live in `caddy-data`.

### Updating

```bash
# Pull latest code, rebuild, and restart
op run --env-file op.env -- docker --context DigitalOcean-remote compose up --build -d
```

Running containers are replaced one at a time. The DB volume is preserved.

### Logs

```bash
# All services
docker --context DigitalOcean-remote compose logs -f

# Bot only
docker --context DigitalOcean-remote compose logs -f bot

# Last 100 lines
docker --context DigitalOcean-remote compose logs --tail=100 bot
```

### Stopping / restarting

```bash
docker --context DigitalOcean-remote compose down     # stop and remove containers
docker --context DigitalOcean-remote compose restart  # restart without rebuild
```

---

## Database migrations

Migrations run automatically on container start (`alembic upgrade head` in the Dockerfile
entrypoint). To generate a new migration after editing `models.py`:

```bash
# Locally, against a local copy of the DB
DATABASE_URL=sqlite:///./data/db/bot.db uv run alembic revision --autogenerate -m "description"

# Apply locally
DATABASE_URL=sqlite:///./data/db/bot.db uv run alembic upgrade head
```

Commit the generated file in `migrations/versions/` and the next deploy applies it.

---

## Dashboard

A read-only observability dashboard at **https://dashboard.exegesis.space** shows:
- Per-board queue depth, today's post count vs cap, and time of last publish
- Last 30 publishes across all boards
- Any submissions currently in a failed state

It auto-refreshes every 2 minutes. No login required.

To run locally (useful for testing queries against a local DB):
```bash
DATABASE_URL=sqlite+aiosqlite:///./data/db/bot.db DATA_DIR=./data uv run python -m bot.dashboard
# open http://localhost:8080
```

---

## Local development

```bash
uv sync --extra dev
uv run pytest        # full test suite
```

---

## Project layout

```
src/bot/
  main.py config.py db.py models.py state.py logging_setup.py
  discord_ingest/   reaction + reply handling, procedural requests
  canonicalize/     per-domain URL canonicalization + known-mirror registry
  asset_store/      attachment download to the persistent volume
  accessibility/    per-image alt-text requirements
  moderation/       NSFW (board-level) + graphic classification
  scheduler/        storage health heartbeat + queue dispatcher
  resolve/          three-tier metadata fetch (oEmbed → mirror OpenGraph → direct)
  publish/          Bluesky publish (external link, image post, native repost)
  queue/            fresh/backlog queue selection logic
  dashboard/        read-only observability web dashboard
  matrix_ingest/ admin/   reserved for later milestones
Caddyfile           reverse proxy config (dashboard.exegesis.space → dashboard:8080)
migrations/         alembic
tests/              canonicalize, resolve, state machine, reply text, queue scheduling
data/               mounted volume (db/, attachments/, logs/) - gitignored
```

---

## Roadmap

- **M1** - Discord ingestion: 🦋 reaction, thread Q&A, source/alt/graphic requests. ✓
- **M2** - Bluesky publishing: external link card + image post + graphic labeling. ✓
- **M3** - Native Bluesky reposts; multi-link reply threads. ✓
- **M4** - Metadata resolvers: oEmbed, mirror OpenGraph, known-domain canonicalization for
  20+ platforms and their mirrors (fxtwitter, vxreddit, kkinstagram, fixdeviantart, etc.),
  path-pattern heuristics for unrecognised mirrors, metadata gap with 🔗 escape hatch,
  duplicate-URL detection, publish-failure retry, hourly queue with fresh/backlog caps,
  read-only web dashboard. ✓
- **M5** - Matrix ingestion adapter (same submission lifecycle, different ingest source).
