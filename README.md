<p align="center">
  <a href="https://exegesis.space">
    <img
      src="https://exegesis.space/exegesis_textlogo.svg"
      alt="Exegesis Logo"
      width="100%"
    />
  </a>
</p>

**Exegesis** is a hard sci-fi, zone-fiction setting about strange people and stranger AI in mechanical bodies on an existentially important mission to investigate a Dyson sphere that is eating at their minds, 112.21 light-years from home. You can read more about it at the [main website](https://exegesis.space).

## NSFW content warning

**Certain Exegesis content includes explicit features, typically tagged as `[evil]`. These elements are opt-in and can be ignored if not outright disabled, but please be aware of their presence. *If you are underage, you obviously shouldn't touch this stuff. Go away.***

---

# exegesis-moodboard-repost-bots

A source-first Discord → Bluesky moodboard bot. Curators react 🦋 on Discord posts; the bot gathers the necessary metadata via a per-submission thread, then schedules posts to Bluesky at a configurable daily cadence.

**Live examples:**
- [robots.exegesis.space](https://bsky.app/profile/robots.exegesis.space) — robot bodies and mechanical forms
- [vehicles.exegesis.space](https://bsky.app/profile/vehicles.exegesis.space) — strange vehicles and transportation
- [doohickeys.exegesis.space](https://bsky.app/profile/doohickeys.exegesis.space) — gadgets and doohickeys
- [memes.exegesis.space](https://bsky.app/profile/memes.exegesis.space) — on-topic memes
- [tv.exegesis.space](https://bsky.app/profile/tv.exegesis.space) — nerd TV
- [xxx-robots.exegesis.space](https://bsky.app/profile/xxx-robots.exegesis.space) — \[evil\] NSFW robots

---

## How it works

1. A curator reacts 🦋 on a Discord post. The bot ingests the message and opens a private thread.
2. The bot parses the message: extracts and canonicalizes URLs, downloads any attached images or videos.
3. If any required data is missing, the bot asks for it in the thread — source URL, replacement images, alt text, graphic content flag — one prompt at a time. Curators and the original poster can both answer.
4. Once all required data is present, the bot posts a preview of the prospective Bluesky post and waits for a ✅ confirmation reaction before queuing.
5. The scheduler posts from the queue once per hour, starting at `QUEUE_START_HOUR`. Each board has a separate daily cap for fresh content (posted within `QUEUE_FRESH_WINDOW_HOURS`) and backlog content.
6. The post format is chosen automatically based on what's available:
   - **Native repost** — for `bsky.app` links
   - **Video embed** — for uploaded video files (transcoded to H.264/AAC via ffmpeg)
   - **Image embed** — for uploaded images (up to 4)
   - **External link card** — for everything else
7. The published Bluesky URL is posted back to the thread, which then archives automatically.
8. Removing 🦋 before publishing cancels the submission. After publishing, the post stays.

---

## Requirements

- Docker and Docker Compose V2
- A Discord bot token (Message Content and Server Members privileged intents required)
- One Bluesky app password per board account
- *(Optional)* [1Password CLI](https://developer.1password.com/docs/cli/) (`op`) for secret management in production
- *(Optional)* YouTube Data API v3 credentials for playlist integration

---

## Setup

### 1. Create a Discord bot

1. Create a new application at [discord.com/developers/applications](https://discord.com/developers/applications)
2. Go to the **Bot** tab and enable these privileged intents:
   - **Message Content Intent**
   - **Server Members Intent**
3. Under **OAuth2 → URL Generator**, select scopes `bot` and the following permissions:
   - View Channels, Read Message History
   - Send Messages, Create Private Threads, Manage Threads
   - Add Reactions
4. Use the generated URL to invite the bot to your server
5. Copy the bot token from the Bot tab — this is your `DISCORD_BOT_TOKEN`

### 2. Set up Bluesky accounts and app passwords

Each board needs its own Bluesky account. Create accounts at [bsky.app](https://bsky.app) (or any PDS). Custom domain handles are configured in Bluesky's own settings and are outside the scope of this setup.

For each account:

1. Log in at bsky.app
2. Go to **Settings → Privacy and Security → App Passwords**
3. Click **Add App Password**, give it a descriptive name (e.g. `repost-bot`), and copy the generated password

App passwords are in the format `xxxx-xxxx-xxxx-xxxx`. They are distinct from the account password and can be revoked independently.

The environment variable name for each board's password is derived from the board's `name` field:

```
BSKY_APP_PASSWORD_<NAME_UPPERCASED_HYPHENS_TO_UNDERSCORES>
```

Examples:
| Board `name` | Env var |
|---|---|
| `my-board` | `BSKY_APP_PASSWORD_MY_BOARD` |
| `weird-wheels` | `BSKY_APP_PASSWORD_WEIRD_WHEELS` |

### 3. Configure boards

`BOARDS_JSON` is a JSON array with one object per watched Discord channel:

```json
[
  {
    "name": "my-board",
    "discord_guild_id": 123456789012345678,
    "discord_channel_id": 123456789012345679,
    "nsfw": false,
    "curator_role_ids": [123456789012345680],
    "curator_user_ids": [],
    "bluesky_handle": "myboard.bsky.social",
    "tags": [],
    "youtube_playlist_id": null,
    "display_name": "My Board"
  }
]
```

| Field | Required | Description |
|---|---|---|
| `name` | yes | Lowercase, hyphen-separated identifier. Determines the `BSKY_APP_PASSWORD_*` env var name. |
| `discord_guild_id` | yes | Server (guild) ID. Enable Developer Mode, then right-click the server → **Copy Server ID**. |
| `discord_channel_id` | yes | The channel to watch. Right-click the channel → **Copy Channel ID**. |
| `nsfw` | no | `true` adds a `sexual` Bluesky content label and an `nsfw` tag to every post. Default `false`. |
| `curator_role_ids` | no | Role IDs whose members can answer bot prompts and confirm posts. |
| `curator_user_ids` | no | Individual user IDs with curator permissions. |
| `bluesky_handle` | yes | The Bluesky account to post to (e.g. `myboard.bsky.social`). |
| `tags` | no | Hashtags appended to every post. |
| `youtube_playlist_id` | no | YouTube playlist ID. If set, YouTube videos are added to this playlist when a submission is confirmed. |
| `display_name` | no | Human-readable board name shown on the dashboard. Defaults to `name`. |

Multiple boards can share the same `discord_guild_id`; each must have a distinct `discord_channel_id`.

### 4. Set up environment variables

```bash
cp .env.example .env
# Fill in .env with your values
```

See [Configuration reference](#configuration-reference) below for documentation on every variable, or refer to the comments in `.env.example` directly.

---

## Configuration reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `DISCORD_BOT_TOKEN` | yes | — | Discord bot token |
| `BSKY_APP_PASSWORD_*` | yes | — | One per board; see setup §2 |
| `BOARDS_JSON` | yes | `[]` | Board config array; see setup §3 |
| `TRIGGER_EMOJI` | no | `🦋` | Reaction emoji that triggers ingestion |
| `DATA_DIR` | no | `/data` | Root directory for the database, attachments, and logs |
| `DATABASE_URL` | no | `sqlite+aiosqlite:////data/db/bot.db` | SQLAlchemy async database URL |
| `STORAGE_MIN_FREE_MB` | no | `500` | Bot pauses ingestion when free disk space falls below this |
| `LOG_LEVEL` | no | `INFO` | Python log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `QUEUE_TIMEZONE` | no | `America/Denver` | IANA timezone name for the posting schedule |
| `QUEUE_START_HOUR` | no | `12` | Hour of day (local time) to begin posting |
| `QUEUE_FRESH_WINDOW_HOURS` | no | `72` | Posts newer than this many hours are considered "fresh" |
| `QUEUE_FRESH_DAILY_CAP` | no | `6` | Max fresh posts published per board per day |
| `QUEUE_BACKLOG_DAILY_CAP` | no | `3` | Max backlog posts published per board per day |
| `CATCHUP_ENABLED` | no | `true` | Scan channel history for missed reactions on bot startup |
| `CATCHUP_LOOKBACK_HOURS` | no | `168` | How far back to scan on startup (hours) |
| `CATCHUP_MAX_MESSAGES` | no | `500` | Max messages to scan per channel on startup |
| `DASHBOARD_URL` | no | — | When set, queued-notice messages in Discord include a link to this URL |
| `YOUTUBE_API_KEY` | no | — | YouTube Data API v3 key (read-only: video titles and thumbnails) |
| `YOUTUBE_CLIENT_ID` | no | — | OAuth2 client ID for playlist writes |
| `YOUTUBE_CLIENT_SECRET` | no | — | OAuth2 client secret |
| `YOUTUBE_REFRESH_TOKEN` | no | — | OAuth2 refresh token |

---

## Deployment

### Build the image

```bash
docker compose build
```

The image is based on `python:3.12-slim`. `ffmpeg` is included for video transcoding. Dependencies are installed via [`uv`](https://github.com/astral-sh/uv).

### Run locally

```bash
docker compose up
```

The container entrypoint runs `alembic upgrade head` before starting the bot, creating and migrating the database automatically on first run.

### Run on a remote server

```bash
# Create a Docker context pointing at your server (one-time setup)
docker context create myserver --docker "host=ssh://user@your-server"

# Build and start in the background
docker --context myserver compose up -d --build
```

### With 1Password CLI (recommended for production)

Secrets are resolved from 1Password at runtime and never written to disk. Copy `.env.example` to `op.env`, replace the plain values with `op://` URIs, then:

```bash
# op.env is .gitignored — never commit it
op run --env-file op.env --no-masking -- docker --context myserver compose up -d --build bot
```

> **Removing op.env from git tracking:** If you previously committed `op.env`, stop tracking it with `git rm --cached op.env` before your next commit. The file will remain on disk but will no longer appear in `git status`.

### Update a running deployment

```bash
# Rebuild and restart one service without touching the other
docker --context myserver compose up -d --build bot
```

### View logs

```bash
docker --context myserver compose logs -f bot
```

### Back up the database

```bash
docker --context myserver compose cp bot:/data/db/bot.db ./backup-$(date +%Y%m%d).db
```

A named Docker volume (`bot-data`) holds the SQLite database and downloaded attachments. It persists across container rebuilds.

---

## Dashboard

The `dashboard` service is a read-only web UI served on port 8080. It shows:

- Queue depth and daily post counts per board
- Submissions awaiting curator input
- Recent publishes with post type and source
- Recent errors from background tasks

It auto-refreshes every two minutes.

```bash
# Local
docker compose up dashboard
# Visit http://localhost:8080
```

For production, proxy port 8080 behind nginx or Caddy with TLS.

---

## Database migrations

Migrations run automatically every time the container starts (`alembic upgrade head` is baked into the entrypoint). No manual steps are needed when upgrading.

To generate a new migration during development:

```bash
DATABASE_URL=sqlite+aiosqlite:///./data/db/bot.db \
  alembic revision --autogenerate -m "describe the change"
```

---

## Development

```bash
uv sync --extra dev
pytest
```

Run the bot directly (requires a populated `.env`):

```bash
uv run python -m bot.main
```

Run the dashboard:

```bash
uv run python -m bot.dashboard
```

---

## Project layout

```
src/bot/
├── main.py                  # Process entrypoint; wires config, DB, Discord client, scheduler
├── config.py                # Pydantic settings; parses BOARDS_JSON into BoardConfig objects
├── models.py                # SQLAlchemy ORM models (submissions, attachments, requests, etc.)
├── state.py                 # Submission state machine and gap detection logic
├── db.py                    # Async engine setup and session factory
├── discord_ingest/          # Discord event handling and submission lifecycle
│   ├── client.py            # discord.py event handlers (reactions, messages, bootup scan)
│   ├── service.py           # DB orchestration: ingest, recompute state, post requests
│   └── replies.py           # All bot message text (centralised, no strings in service.py)
├── publish/                 # Bluesky post creation
│   └── __init__.py          # Formats and publishes posts; handles all embed types
├── canonicalize.py          # URL canonicalization and domain family detection
├── resolve.py               # Metadata resolution (oEmbed, OpenGraph, HTML, Discord embeds)
├── accessibility/           # Alt text utilities and attachment type detection
├── asset_store.py           # File download, local storage management, disk space checks
├── moderation/              # Graphic content classification helpers
├── queue.py                 # Posting cadence: daily caps, fresh vs. backlog logic
├── scheduler.py             # Background tasks: housekeeping loop and queue dispatcher
├── youtube.py               # YouTube Data API client (metadata + playlist writes)
└── dashboard/               # Read-only FastAPI web UI
    ├── __init__.py           # FastAPI app and route handlers
    ├── queries.py            # Read-only DB queries for the dashboard
    └── templates/           # Jinja2 HTML templates

migrations/                  # Alembic migration scripts
tests/                       # pytest test suite
data/                        # Runtime volume (db/, attachments/) — not committed
```
