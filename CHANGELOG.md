# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- DID pinning for Bluesky sources: the source post's permanent DID is resolved and stored (`source_at_uri` on `submission_links`) at ingest, while the handle is still live, so a later handle rename or deactivation can no longer break the repost
- `bot.admin.backfill_bsky_did` one-shot to pin DIDs onto submissions ingested before the change

### Fixed
- Bluesky reposts failing permanently with "Unable to resolve handle" when a source account renamed or deactivated its handle between submission and publish: publishing now prefers the pinned DID and only falls back to live handle resolution for legacy rows

## [1.0.0] - 2026-06-23

### Added
- Discord ingest bot: 🦋 reaction on a channel message opens a private thread, fetches metadata, requests alt text and graphic classification from curators, and queues the submission for publishing
- Bluesky publishing via ATProto: native reposts for Bluesky-sourced links; image posts (up to 4 images with alt text) for everything else
- Hourly queue dispatcher: fires from noon MT, distinguishes fresh (<=72h old, up to 6/day) from backlog (up to 3/day) submissions per board
- URL canonicalization for Reddit, Twitter/X, YouTube, Bluesky, Instagram, DeviantArt, Tumblr, Pixiv, Flickr, Wikipedia, ArtStation, and common mirrors
- Web dashboard at `dashboard.exegesis.space`: per-board cards (queue depth, daily cap, fresh/backlog mode, last post), recent publishes table, per-board queue detail page
- Recent errors section on dashboard: scheduler failures and other background exceptions are persisted to `bot_errors` table and shown with expandable tracebacks
- Alt text requests include the image as a Discord file attachment so curators can see what they are alt-texting
- Image attachments resized to 1920px max and re-compressed before Discord upload to stay within the 8 MB limit
- Catch-up mode on bot start: scans recent channel history and ingests any missed 🦋 reactions
- Per-board Bluesky credentials via `BSKY_APP_PASSWORD_<BOARD>` in 1Password
- Secrets injection via `op run --env-file op.env` at runtime; `op.env` safe to commit
- SQLite write serialization via `asyncio.Lock` to prevent "database is locked" errors under concurrent Discord events and scheduler ticks
- WAL mode for SQLite
- Alembic migrations
- Semantic versioning; version shown in dashboard header and as Discord bot activity ("Watching vX.Y.Z")
- Dashboard timestamps displayed in Mountain Time

### Fixed
- Submissions stuck in `ready_to_queue` state due to unconditional state overwrite and incorrect terminal-state guard in `recompute_and_request`
- Queue page 500 error from naive/aware datetime comparison (`source_posted_at` stored naive in SQLite, compared against aware cutoff)
- Scheduler failing silently for all boards due to `SubmissionThread.submission_id` attribute not existing (correct lookup is by `board_id` + `source_discord_message_id`)
- nerd-tv board incorrectly showing backlog mode: YouTube submissions have no `source_posted_at`, so freshness now falls back to `created_at` via `COALESCE`
- Discord 413 Payload Too Large when sending high-resolution images to Discord for alt text review

[Unreleased]: https://github.com/choccccy/exegesis-moodboard-repost-bots/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/choccccy/exegesis-moodboard-repost-bots/releases/tag/v1.0.0
