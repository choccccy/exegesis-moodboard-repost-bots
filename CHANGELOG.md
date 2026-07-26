# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- Surface-agnostic core Phase C (issue #58): the interaction handlers no longer touch Discord's interaction-response API. A curation handler now takes a normalized `InteractionEvent` (`bot.ingest.events`) plus a `Surface`, and returns a `HandlerOutcome` (`bot.ingest.outcomes`: `Ack` / `OpenModal` / `Tombstone` / `Noop`); a Discord `gateway` (`discord_ingest/gateway.py`) builds the event, runs the handler in a short DB scope, and performs the outcome (modal/ack/tombstone) with the DB lock released. All 10 button/select handlers are migrated (edit, alt-edit, alt-pick, cancel, confirm, metadata-confirm, graphic, playlist-skip, source-note confirm/reject) and carry no `discord` import - their thread-side work runs through the `Surface` port (which gained a delay-parameterized `archive_after_delay` for playlist-skip's thread-close re-arm). No user-visible behaviour change. Physical relocation of the now-agnostic core into `bot/curation/` and the Matrix adapter remain as Phase D.
- Surface-agnostic core Phase B (issue #57): retired `discord_ingest/views.py`. Every button/select/modal is now built from a surface-agnostic descriptor (`bot.components` via `discord_ingest/prompts.py`) and rendered in one place (`discord_ingest/render.py`, now with `render_modal` for the edit-post/alt-text modals). The interaction handlers no longer construct `discord.ui` widgets directly. `action_id` stays byte-identical to the old `custom_id` scheme, so `on_interaction` routing and every persisted button are unaffected. No user-visible behaviour change. Also backfilled test coverage on the Phase A adapters (`DiscordSurface`, `NullSurface`, `render`, `prompts`) that were previously only partially exercised.
- Surface-agnostic outbound core (groundwork for a future Matrix surface, issue #50): the curation service layer now posts through a platform-agnostic `Surface` port with abstract component descriptors (`bot.components`) instead of building `discord.ui` views/files directly; a `DiscordSurface`/`render.py` adapter renders them. `recompute_and_request` was made self-managing (a short DB scope decides, Discord sends run with the lock released, a short DB scope persists), serialized by a new per-submission lock. As a result the "no Discord/network I/O under the DB write lock" invariant now holds for the whole `handle_reaction` path (previously a documented exception). No user-visible behaviour change. Interaction-handler decoupling (buttons/slash/modals) and the inbound event vocabulary remain follow-ups.
- Responsiveness under load: the bot no longer stalls (acknowledging interactions but actioning them seconds later) while creating many threads during a butterfly storm. Network and Discord I/O that used to run while holding the global SQLite write lock now runs with the lock released. Specifically: thread creation + anchor posting, link-metadata resolution, thumbnail/attachment/video downloads, and the entire Bluesky publish conversation are all lifted out of the DB lock. `handle_reaction`, `publish_queued_submission`, `reingest_submission`, and the scheduler/threadless-retry paths are now self-managing (they open short DB transactions around lockless I/O). Behaviour is otherwise unchanged; see `docs/db-lock-io-refactor.md`. (Remaining under-lock I/O in the interaction handlers is fast and human-paced; de-locking it was deliberately deferred.)

### Added
- DID pinning for Bluesky sources: the source post's permanent DID is resolved and stored (`source_at_uri` on `submission_links`) at ingest, while the handle is still live, so a later handle rename or deactivation can no longer break the repost
- `bot.admin.backfill_bsky_did` one-shot to pin DIDs onto submissions ingested before the change

### Fixed
- TikTok embed-fixer mirrors ingested as generic links (issue #53): `tnktok.com` (plus `vxtiktok.com` and the official `vt.tiktok.com` short domain) are now recognized as the TikTok family and canonicalized to the real `www.tiktok.com/@user/video/ID`, so they resolve to the video's cover/metadata instead of a mangled mirror-page card
- Double post / double thread anchor (issue #52): during a butterfly storm, thread creation is rate-limited, so a submission can sit "threadless" (its `SubmissionThread` mapping not yet persisted) for minutes while `handle_reaction` is still creating its thread. The periodic threadless-retry loop did not take the per-message processing lock, so it read that submission as threadless and created a *second* thread + anchor. The retry loop now acquires the same per-message lock and re-checks the mapping under it, skipping submissions another path has already threaded
- Discord navigation links are no longer mistaken for source content (issue #51): jump-to-message / channel links and invites (`discord.com/channels/…`, `discord.gg/…`) are dropped from source-URL extraction at every stage, so a quoted or forwarded message that carries both a Discord link and the real source URL ingests only the real one (CDN/media hosts like `cdn.discordapp.com` are kept)
- `/reingest` now posts its confirmation publicly in the thread instead of ephemerally (issue #55), so curators can see that a submission was refreshed
- Bluesky publish no longer fails with an illegible "list index out of range" (issue #56): resolving a bare profile link or otherwise malformed bsky URL to a repostable post now raises a clear "not a Bluesky post URL" error instead of an `IndexError`
- Bluesky reposts failing permanently with "Unable to resolve handle" when a source account renamed or deactivated its handle between submission and publish: publishing now prefers the pinned DID and only falls back to live handle resolution for legacy rows
- Transient Bluesky login failures no longer waste a queue slot: `client.login` now retries a few times with short backoff before giving up, so a momentary connection drop or timeout to `createSession` is absorbed in-process instead of deferring the submission to the next hourly tick
- Publish-failure reports now include the exception type (e.g. `login failed: ConnectTimeout`) instead of a blank when the underlying error stringifies to nothing, and the error is shown in a copyable Discord code block

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
