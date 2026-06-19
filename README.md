- https://bsky.app/profile/robots.exegesis.space
- https://bsky.app/profile/xxx-robots.exegesis.space
- https://bsky.app/profile/vehicles.exegesis.space
- https://bsky.app/profile/doohickeys.exegesis.space
- https://bsky.app/profile/tv.exegesis.space

# Discord → Bluesky Repost Bot

A source-first bot that turns curated Discord moodboard posts into Bluesky reposts
while always preserving the canonical source URL and image alt text.

**Milestone 1 (this build): Discord ingestion only.** A 🦋 reaction creates a
tracked submission; the bot canonicalizes links, downloads attachments to a
persistent volume, and asks in-thread for any missing source URL / alt text /
graphic classification. Submissions wait indefinitely until complete. **No Bluesky
publishing yet** - that is Milestone 2, and no Bluesky credentials are needed now.

See `bluesky-repost-bot-plan.md` for the full product specification.

## How it works (M1)

1. Someone posts a link and/or image attachments in a watched channel.
2. Someone reacts with 🦋 → the bot creates a `submission` and opens a **thread** off
   that message where all the procedural Q&A happens (keeps the main channel clean).
3. The bot parses + canonicalizes URLs (strips trackers; preserves YouTube timestamps),
   downloads attachments to `/data/attachments/...`, and reuses Discord alt text when present.
4. For anything missing it posts one procedural request per gap in the thread:
   - `reply to this message with the source URL`
   - `reply to this message with the alt text for **<file>**`
   - `reply to this message with whether this should be marked graphic (yes/no)`
5. The original poster **or any curator-role user** replies in the thread; the bot records
   the answer and re-evaluates. When all requirements are met it posts the `ready_to_queue`
   confirmation followed by a verification preview of everything it holds.
6. Removing the 🦋 deletes the submission (and its thread/files); re-adding 🦋 starts fresh.

## Requirements

- Docker + Docker Compose
- [1Password CLI](https://developer.1password.com/docs/cli/) (`op`) signed in
- For local development/tests: Python 3.12 + [`uv`](https://docs.astral.sh/uv/)

## Configuration

All config comes from environment variables (see `.env.tmpl` for the full reference).
The real `.env` is generated from the template via `op inject` so no secret touches source.

Key values:

| Var | Meaning |
|---|---|
| `DISCORD_BOT_TOKEN` | Bot token (1Password `op://` reference) |
| `BOARDS_JSON` | JSON list of boards: `name`, `discord_guild_id`, `discord_channel_id`, `nsfw`, `curator_role_ids` |
| `TRIGGER_EMOJI` | Defaults to 🦋 |
| `DATABASE_URL` | SQLite path on the volume |
| `STORAGE_MIN_FREE_MB` | Free-space floor below which downloads pause |

## Discord setup (one-time)

1. Create an app + bot at <https://discord.com/developers/applications>.
2. Under **Bot → Privileged Gateway Intents**, enable **Message Content Intent**
   (and Server Members Intent).
3. Invite the bot with scope `bot` and permissions: View Channels, Read Message
   History, Add Reactions, Send Messages, Send Messages in Threads, Attach Files,
   **Create Public Threads**, and **Manage Threads** (the bot runs each submission's
   procedural Q&A in a thread and deletes that thread when the 🦋 is removed).
4. Put the bot token in 1Password and point `DISCORD_BOT_TOKEN` in `.env.tmpl` at it.
5. Fill `BOARDS_JSON` with your guild ID, the channel ID to watch, and curator role IDs
   (enable Developer Mode in Discord to copy IDs).

## Run

```bash
# 1. Materialize secrets/config from 1Password (never commit the result)
op inject -i .env.tmpl -o .env

# 2. Build + start (migrations run automatically on container start)
docker compose up --build
```

The container runs `alembic upgrade head` then launches the bot. Data (SQLite DB,
downloaded attachments, logs) lives in the named `bot-data` volume and survives rebuilds.

## Local development

```bash
uv sync --extra dev
uv run pytest                      # unit tests (canonicalization + state machine)

# generate / update DB migrations after editing models.py
DATABASE_URL=sqlite:///./data/db/bot.db uv run alembic revision --autogenerate -m "msg"
DATABASE_URL=sqlite:///./data/db/bot.db uv run alembic upgrade head
```

## Project layout

```
src/bot/
  main.py config.py db.py models.py state.py logging_setup.py
  discord_ingest/   reaction + reply handling, procedural requests
  canonicalize/     per-domain URL canonicalization registry (+ tests)
  asset_store/      attachment download to the persistent volume
  accessibility/    per-image alt-text requirements
  moderation/       NSFW (board-level) + graphic classification
  scheduler/        storage health heartbeat
  resolve/          metadata fetch - STUB (M4)
  queue/ publish/ matrix_ingest/ admin/   reserved for later milestones
migrations/         alembic
tests/              canonicalize + state machine
data/               mounted volume (db/, attachments/, logs/) - gitignored
```

## Roadmap

- **M2** - authenticate one Bluesky account; publish eligible non-Bluesky items (canonical URL + uploaded images + alt text). ✓
- **M3** - Bluesky-native reposts; multi-link reply threads. ✓
- **M4** - platform-specific metadata resolvers; graphic-content labeling.
- **M5** - Matrix ingestion adapter (same user rules).
