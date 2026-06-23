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
   prospective Bluesky post, then publishes immediately.
6. For Bluesky posts, the bot reposts natively (using the AT Protocol `repost` record) rather
   than creating a new post. For everything else it creates an external-link card or image post.
7. Removing the 🦋 deletes the submission and its thread. Published posts cannot be un-reacted
   (the Bluesky post is already live — contact an admin to take it down).
8. If publish fails, the bot retries automatically on restart. Another curator can also re-react
   🦋 to trigger a retry without restarting.

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
| `BOARDS_JSON` | JSON list of boards: `name`, `discord_guild_id`, `discord_channel_id`, `nsfw`, `curator_role_ids`, `bluesky_handle`, `tags` |
| `BSKY_APP_PASSWORD_<NAME>` | App password for each board's Bluesky account |
| `TRIGGER_EMOJI` | Defaults to 🦋 |
| `DATABASE_URL` | SQLite path on the volume |
| `STORAGE_MIN_FREE_MB` | Free-space floor below which downloads pause |
| `CATCHUP_ENABLED` | Re-scan recent history on startup to catch missed 🦋 reactions |

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
   (enable Developer Mode in Discord to copy IDs). Add `bluesky_handle` and a `tags` list
   for each board.
6. Create a Bluesky app password for each board at <https://bsky.app/settings/app-passwords>
   and add it to 1Password. Wire it in `.env.tmpl` as `BSKY_APP_PASSWORD_<BOARD_NAME_UPPER>`.

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
uv run pytest                      # unit tests

# generate / update DB migrations after editing models.py
DATABASE_URL=sqlite:///./data/db/bot.db uv run alembic revision --autogenerate -m "msg"
DATABASE_URL=sqlite:///./data/db/bot.db uv run alembic upgrade head
```

## Project layout

```
src/bot/
  main.py config.py db.py models.py state.py logging_setup.py
  discord_ingest/   reaction + reply handling, procedural requests
  canonicalize/     per-domain URL canonicalization + known-mirror registry
  asset_store/      attachment download to the persistent volume
  accessibility/    per-image alt-text requirements
  moderation/       NSFW (board-level) + graphic classification
  scheduler/        storage health heartbeat
  resolve/          three-tier metadata fetch (oEmbed → mirror OpenGraph → direct)
  publish/          Bluesky publish (external link, image post, native repost)
  queue/ matrix_ingest/ admin/   reserved for later milestones
migrations/         alembic
tests/              canonicalize, resolve, state machine, reply text
data/               mounted volume (db/, attachments/, logs/) - gitignored
```

## Roadmap

- **M1** - Discord ingestion: 🦋 reaction, thread Q&A, source/alt/graphic requests. ✓
- **M2** - Bluesky publishing: external link card + image post + graphic labeling. ✓
- **M3** - Native Bluesky reposts; multi-link reply threads. ✓
- **M4** - Metadata resolvers: oEmbed, mirror OpenGraph, known-domain canonicalization for
  20+ platforms and their mirrors (fxtwitter, vxreddit, kkinstagram, fixdeviantart, etc.),
  path-pattern heuristics for unrecognised mirrors, metadata gap with 🔗 escape hatch,
  duplicate-URL detection, publish-failure retry. ✓
- **M5** - Matrix ingestion adapter (same submission lifecycle, different ingest source).
