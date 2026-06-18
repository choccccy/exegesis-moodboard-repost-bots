# Discord-to-Bluesky Repost Bot Implementation Plan

## Overview

This document specifies a standalone Dockerized Python bot that ingests posts from Discord moodboard channels and republishes them to Bluesky while preserving attribution and pushing attention back to the original source.[web:111][web:128] The bot is intentionally **source-first**, not a content aggregator: every repost must include the canonical source URL, mirrors are fetch helpers only, and missing source information blocks publication until a human supplies it.[web:118][web:132][web:143]

The system is Discord-first for v1, but it must be designed so Matrix can later become a parallel or replacement ingestion surface without changing the user-facing rules. The same concepts — reaction-driven submission, bot replies asking for missing information, attachment handling, approval gating, and queueing — should later map onto Matrix with minimal behavioral drift.[web:99]

## Core principles

### Source transparency

- The canonical source URL must **always** appear in the Bluesky post or Bluesky-native repost target. Mirrors must never be used as the public-facing posted URL.
- If the Discord poster attached images because the platform embed was poor, those attached images should be treated as the intended visual payload for the repost, but they do not replace the need for the canonical source URL.
- If the bot cannot identify a canonical source URL with confidence, it must ask for clarification in-thread and wait indefinitely.

### Accessibility

- Every non-Bluesky-native image upload requires alt text for **every** image before posting.[web:119]
- If alt text is inferable, the bot may generate a draft, but human-provided alt text is still preferred and the candidate should remain blocked until the required alt text exists.
- Bluesky-native reposts are the one practical exception: if the platform-native repost mechanism does not permit the bot to impose per-image alt text, that repost type is still allowed because it preserves the original post and its existing accessibility state.

### Human-in-the-loop curation

- A 🦋 reaction means **intent to submit**, not approval to publish.
- Missing information must be requested by the bot in-thread using simple procedural prompts.
- The bot may @ping the original poster, but replies may come from any authorized curator role.
- Incomplete submissions remain pending forever rather than expiring.

### Low editorialization

- The bot should generally avoid commentary.
- For non-Bluesky posts, use the source page title when available; otherwise use minimal neutral text plus the canonical URL.[web:103][web:115]
- For Bluesky posts, prefer platform-native repost behavior whenever possible.[web:111][web:128]

## Product shape

The bot should support **one Bluesky account per moodboard**. Each account corresponds to one Discord moodboard channel and later, optionally, one Matrix room. This keeps account identity legible and lets followers subscribe to specific themes without needing to understand the whole Exegesis ecosystem.

The bot is not a “starter pack” generator, but it is compatible with that ecosystem goal: these accounts can later be grouped into Starter Packs for onboarding people into the topic network.[web:104][web:107]

## Supported submission flow

### Discord as source of truth in v1

V1 should ingest live Discord channel activity directly through a Discord bot/app, including message content, reactions, replies, author IDs, attachments, and thread replies. Discord bot setup therefore requires message content access, attachment access, and per-channel monitoring permissions.

### Trigger model

1. A user posts a link, attachments, or both in a watched moodboard channel.
2. A user reacts with 🦋.
3. The bot parses the message and creates a `pending_submission`.
4. The bot determines whether it has:
   - a canonical source URL,
   - enough metadata to classify the source,
   - alt text for each attached image,
   - any required content-warning data.
5. If anything is missing, the bot replies in-thread with procedural requests.
6. Once all required information exists, the candidate becomes eligible for queueing and later publication.

## Required posting rules

### Hard requirements

A candidate may not be published unless all of the following are true:

- The canonical source URL is known.
- All non-Bluesky-native uploaded images have alt text.
- The item has passed any NSFW/graphic labeling requirements.
- The item is not ambiguous about which source is primary.

### Special rule for multiple links

If a single submitted message resolves to multiple canonical links:

- The first canonical link becomes the top-level Bluesky post.
- Each additional canonical link becomes a reply in the same Bluesky thread.
- Each reply follows the same source, accessibility, and metadata rules as the first post.

If the primary link is ambiguous, the bot must ask for clarification before any posting begins.

## Canonical URL policy

The bot should maintain a formal URL canonicalization registry with per-domain rules. This registry is part of the product, not an implementation detail.

### General rules

- Strip tracking parameters wherever possible.
- Prefer stable, elegant canonical URLs.
- Preserve parameters only when they materially change what the source points to.
- Mirrors are valid fetch helpers but never canonical public output.

### Domain-specific rules

| Domain family | Canonical output rule | Notes |
|---|---|---|
| Bluesky | Use `https://bsky.app/profile/.../post/...` | Bluesky-native reposts should target the real Bluesky post.[web:111] |
| Reddit | Use canonical `https://www.reddit.com/...` URL | Mirrors may assist fetches, but the canonical Reddit link must be posted.[web:118] |
| Twitter/X | Normalize to `https://twitter.com/.../status/...` | Mirrors like `fxtwitter`, `fixupx`, and similar are fetch helpers only. |
| ArtStation | Use `https://www.artstation.com/artwork/...` | Canonical link is generally good; Discord attachments may supplement visuals. |
| DeviantArt | Use `https://www.deviantart.com/...` | Helper sites may assist preview extraction; canonical public URL remains DeviantArt.[web:143] |
| Wikipedia | Normalize to desktop canonical article URL | Mobile variants should collapse to canonical article form.[web:145] |
| Instagram | Use `https://www.instagram.com/p/.../` | Mirrors and official oEmbed/API are fetch options only.[web:132] |
| YouTube | Normalize to `https://youtu.be/<video_id>` | Strip trackers and playlist params; preserve timestamps when present.[web:120][web:123] |

### Tracking stripping

The canonicalizer should strip common trackers such as `si`, `utm_*`, and comparable share parameters where they do not change the identity of the source. The one explicit exception already identified is YouTube timestamps, which should be preserved because they often represent intentional source context.[web:120][web:123]

## Fetch strategy

The fetch pipeline should be explicitly tiered.

### Ordered resolution strategy

For each source URL, the bot should try in this order:

1. Parse and normalize the canonical URL.
2. Attempt metadata fetch from the canonical URL itself.
3. Attempt official oEmbed or official API endpoints where applicable.[web:132][web:143]
4. Attempt fetch-helper mirrors if the canonical source is poor for previews.
5. Attempt limited scraping where legally and technically sensible.
6. If the needed information still cannot be determined, ask the Discord poster or another curator in-thread.

The output URL remains canonical regardless of which helper source produced the preview metadata.

### Why this matters

This policy is especially important for Reddit, Instagram, Twitter/X, and DeviantArt, where direct scraping quality or reliability is poor, and in Reddit’s case unauthenticated access has become more restricted.[web:118][web:121][web:132][web:143]

## Platform-specific handling

### Bluesky

For Bluesky links, the bot should prefer a native Bluesky repost / quote-equivalent strategy instead of creating a synthetic duplicate post wherever possible.[web:111][web:128] This is the cleanest and most transparent behavior because the source object already exists inside the target platform.

The implementation plan should explicitly note that Bluesky post composition has tradeoffs between record embeds, image embeds, and external embeds, so the publisher layer must choose the correct embed mode instead of assuming every post can combine everything at once.[web:128][web:149]

### Reddit

Reddit canonical links must be posted as the source URL, but fetch quality is likely to degrade because Reddit has tightened unauthenticated access paths.[web:118][web:121] The bot should therefore:

- try canonical fetch,
- try official authenticated paths if available for the project,
- try helper mirrors when useful,
- then fall back to the original Discord poster for missing details.

The system should not attempt elaborate bypass behavior; durable fallback-to-human is the right design.

### Twitter/X

Twitter/X links should canonicalize to `twitter.com`, not `x.com`, per project preference. Helper mirrors like `fixupx`, `fxtwitter`, and related domains should only be used for fetch assistance, never as the posted URL.

### ArtStation

ArtStation canonical links are often good enough as source URLs and may provide thumbnail-level metadata, but Discord attachments may still represent the real desired image set. The source link remains ArtStation; the visual payload may be native uploaded images taken from Discord.

### DeviantArt

The bot should try the canonical DeviantArt URL first, then official oEmbed where useful, then helper mirrors, then scraping, then human fallback.[web:143] The final posted source URL remains the canonical DeviantArt link.

### Wikipedia

Wikipedia links should normalize to the desktop canonical form, including converting mobile-form variants when encountered.[web:145] Wikipedia links are otherwise straightforward and usually produce acceptable metadata.

### Instagram

The bot should try canonical fetch, then official Meta/Instagram oEmbed or API options if configured, then helper mirrors, then scraping, then human fallback.[web:132][web:141] Instagram is expected to fail often enough that the information-request workflow is a first-class feature, not an exception.[web:132][web:138]

### YouTube

YouTube links should normalize into `youtu.be/<id>` form while preserving timestamps and dropping playlist/tracker junk.[web:120][web:123] Canonical YouTube metadata is generally usable.

## Attachments policy

### Discord attachments as native Bluesky uploads

If the Discord source message includes image attachments and the item is otherwise eligible, those images should be downloaded by the bot and uploaded natively to Bluesky rather than linked back to Discord CDN URLs. This is necessary because Discord attachment links are not reliable long-term external references and attachment URLs may expire or require refresh behavior.[web:150][web:156][web:159]

### Important storage rule

The bot must not treat Discord CDN links as durable asset references. It should download attachments promptly into a persistent data volume outside the container and use those stored files for later publication or re-publication.[web:156][web:159]

### Attachment reply workflow

For each image attachment lacking alt text, the bot should post a separate procedural reply:

> reply to this image with the alt text

For missing canonical URL, the bot should post a separate procedural reply:

> reply to this message with the source URL

This keeps input dead simple and avoids forcing curators to learn a mini command language.

## Accessibility workflow

### Alt text requirements

- Every uploaded image requires alt text before posting.[web:119]
- Alt text should be stored per attachment, not per submission.
- If a submission has four images, all four need alt text before the submission can move to `ready_to_queue`.
- No timeout exists; wait indefinitely.

### Draft assistance

The architecture should leave hooks for optional future alt-text assistance, but OCR or image-description automation is out of scope for v1. The bot may optionally suggest draft alt text if trivial inference is possible, but the required operational design is still human-supplied text.

### Discord data note

Discord attachments themselves may include a description field in some API surfaces, which should be checked and reused if present before asking humans again.[web:153]

## Content labeling and moderation

### NSFW board policy

One moodboard is assumed to be NSFW for sexual content. Every post originating from that board should be labeled accordingly in the bot’s policy layer.

### Graphic content

Graphic/gory distinctions are not currently encoded in the source channels, so when relevant the bot should ask the original poster or an authorized curator whether a submission is graphic. The answer should be stored as structured moderation metadata and carried forward into publishing decisions.

### Bluesky moderation caveat

Bluesky labels and moderation behaviors are mediated through moderation services and platform capabilities, so the implementation plan should not assume arbitrary self-label semantics are always available in the same way for every post type.[web:160][web:154][web:148] The publisher layer should encapsulate whatever label-setting is actually supported for the account/workflow in use.

## Approval and pending-state model

A 🦋 reaction creates a candidate in `intent_submitted` state. It should not be treated as ready for publication.

### Recommended states

| State | Meaning |
|---|---|
| `intent_submitted` | A user reacted with 🦋 and the bot created a candidate. |
| `awaiting_source` | Canonical URL is missing or ambiguous. |
| `awaiting_alt_text` | One or more attached images still need alt text. |
| `awaiting_graphic_classification` | Graphic/gore status needs human input. |
| `ready_to_queue` | All requirements are satisfied. |
| `queued` | Eligible and scheduled. |
| `published` | Successfully posted to Bluesky. |
| `publish_failed` | Attempted but failed; retryable. |

Candidates should remain in their pending state indefinitely until completed or manually rejected.

## Human reply parsing

The reply parser should be extremely simple.

### Supported interaction model

- For source requests, the bot expects a direct reply containing a URL.
- For alt text requests, the bot expects a direct reply containing the alt text.
- For graphic-content requests, the bot expects a direct reply containing a simple yes/no or equivalent recognized token.

Authorized curators should be configurable by Discord role so they can satisfy requests even if the original poster does not.

## Posting format

### Non-Bluesky source post

Preferred output structure:

- Minimal text, ideally the source title if available.[web:103][web:115]
- Canonical source URL.
- Native-uploaded images from Discord attachments, if any, with alt text.

This keeps the post elegant while still making the source explicit.

### Bluesky source post

Preferred output structure:

- Native Bluesky repost behavior whenever possible.[web:111][web:128]
- If a direct native repost is not possible under some edge condition, fall back to the canonical Bluesky URL rather than copying the source content into a new post.

### Multi-link source post

- First canonical link becomes top-level post.
- Remaining canonical links become replies in-thread.
- Each reply should remain minimal and source-explicit.

## Queueing strategy

The existing fresh-vs-backlog plan still applies, but queue eligibility must now incorporate stricter gating.

### Eligibility rule

Only `ready_to_queue` items may enter the fresh or backlog scheduler. Missing source URLs, missing alt text, or unresolved content-label requirements block queue entry entirely.

### Priority rule

- Prefer fresh eligible submissions when under per-board posting caps.
- Fall back to approved backlog items when no fresh item is eligible.
- If nothing is eligible on a given day, simply do not post.

This matches the project preference for correctness over filling quota.

## High-level architecture

The bot should still be a single Python application in one Docker container, but its interfaces need to reflect the Discord-first ingestion model and later Matrix compatibility.

### Recommended modules

| Module | Responsibility |
|---|---|
| `discord_ingest` | Watches configured Discord channels, reactions, replies, threads, and attachments. |
| `matrix_ingest` | Future adapter mirroring the same behavior for Matrix rooms.[web:99] |
| `canonicalize` | Converts raw links into canonical forms and strips trackers. |
| `resolve` | Fetches metadata from canonical URLs, official APIs/oEmbed, mirrors, and scraping fallbacks.[web:132][web:143] |
| `asset_store` | Downloads and persists Discord attachments outside the container. |
| `accessibility` | Tracks per-image alt text requests and fulfillment. |
| `moderation` | Tracks NSFW/graphic classification and curator permissions. |
| `queue` | Fresh/backlog selection once items are fully ready. |
| `publisher` | Handles Bluesky-native reposts, external-link posts, and image uploads.[web:111][web:119][web:128] |
| `admin` | CLI and maintenance commands. |
| `scheduler` | Periodic jobs for sync, retry, queue evaluation, and reporting. |

## Data model

The schema should evolve from the earlier bot plan, with Discord-first fields and explicit request-tracking.

### Core tables

| Table | Purpose |
|---|---|
| `boards` | One record per moodboard / Bluesky account. |
| `discord_messages` | Source Discord message metadata. |
| `submissions` | One submission created by a 🦋 reaction. |
| `submission_links` | One canonicalized source link per submission, in thread order. |
| `attachments` | Downloaded Discord attachments and per-file metadata. |
| `attachment_alt_text_requests` | Tracks bot prompts and replies for alt text. |
| `source_requests` | Tracks bot prompts and replies for canonical URL clarification. |
| `content_label_requests` | Tracks bot prompts and replies for graphic-content classification. |
| `publish_attempts` | Audit log of posting attempts and returned IDs/URIs. |
| `curators` | Authorized Discord users and/or roles allowed to answer bot requests. |
| `dedupe_keys` | Canonical-link and content dedupe information. |

### Important attachment fields

- local storage path
- original Discord attachment URL
- MIME type
- width/height if image
- spoiler flag if present
- alt text status
- alt text body
- alt text author
- downloaded timestamp

## Discord bot behavior specification

### Watched channels

Each board should map to one watched Discord channel. The bot needs channel IDs, guild ID, and a role configuration for curators.

### Reaction handling

On 🦋 reaction:

1. Verify the channel is watched.
2. Create or update the submission.
3. Parse URLs from message content.
4. Normalize candidate canonical links.
5. Download attachments to local storage.
6. Check whether the candidate has enough information.
7. Emit targeted request replies for missing fields.

### Procedural request examples

For missing source:

> @user reply to this message with the source URL

For missing alt text:

> @user reply to this image with the alt text

For unresolved graphic content:

> @user reply to this message with whether this should be marked graphic

Messages should be utilitarian and procedural, not chatty.

## Matrix compatibility hooks

The later Matrix implementation should preserve the same user mental model:

- reaction or equivalent signal means intent to submit,
- the bot asks for missing source URL or alt text in replies/threads,
- curator roles map to Matrix power/role concepts,
- the same queueing and publishing rules apply.

That means the core submission model must be platform-neutral even though Discord is the v1 source of truth.

## Docker and persistence design

The bot remains a standalone container with mounted persistent storage.

### Required persistent volumes

- database storage
- downloaded attachment storage
- fetch/cache storage
- logs

This is specifically necessary because Discord attachment URLs are not suitable as durable long-term references and the bot must keep locally controlled copies for restarts and later publish jobs.[web:156][web:159]

### Failure behavior

If local storage fills up, the bot should fail gracefully:

- stop downloading new attachments,
- mark affected submissions as blocked by storage,
- emit operator-visible alerts,
- avoid losing DB state.

## Recommended project structure

```text
bluesky-repost-bot/
├── docker-compose.yml
├── Dockerfile
├── .env.example
├── README.md
├── pyproject.toml
├── src/
│   └── bot/
│       ├── main.py
│       ├── config.py
│       ├── db.py
│       ├── models.py
│       ├── discord_ingest/
│       ├── matrix_ingest/
│       ├── canonicalize/
│       ├── resolve/
│       ├── asset_store/
│       ├── accessibility/
│       ├── moderation/
│       ├── queue/
│       ├── publish/
│       ├── scheduler/
│       └── admin/
├── migrations/
└── data/
```

## Suggested implementation order

### Milestone 1

- Discord bot reads one watched channel.
- 🦋 reaction creates a submission.
- URLs are canonicalized.
- Attachments are downloaded locally.
- Bot requests missing source URL and alt text in replies.
- No Bluesky publishing yet.

### Milestone 2

- One Bluesky board account is authenticated.
- Eligible non-Bluesky items can publish with canonical URL plus uploaded images and alt text.[web:119][web:111]
- Publish attempts are audited.

### Milestone 3

- Bluesky-native repost path is implemented for Bluesky source links.[web:111][web:128]
- Multi-link submissions become reply threads.

### Milestone 4

- Platform-specific resolvers improve fetch quality for Reddit, Instagram, DeviantArt, YouTube, Wikipedia, Twitter/X, and ArtStation.[web:118][web:132][web:143][web:145]
- Graphic-content prompts and label handling are added.

### Milestone 5

- Matrix ingestion adapter is added without changing submission semantics.[web:99]

## What the outside agent should deliver

The outside agent should deliver a durable baseline, not a proof-of-concept script.

### Deliverables

- Python application packaged as one Docker container.
- Discord bot integration with watched-channel configuration.
- Bluesky publisher integration using the Python AT Protocol client or equivalent supported approach.[web:111][web:119][web:128]
- Persistent attachment download storage.
- Canonicalization registry with per-domain rules.
- Procedural reply/request workflow for missing source URL, alt text, and graphic classification.
- Queueing system that only admits fully-satisfied candidates.
- README and operator docs.
- `.env.example` and setup instructions for Discord and Bluesky credentials.
- Explicit notes about future Matrix adapter compatibility.

## Success criteria

This project is successful when all of the following are true:

- Every published post clearly points back to the canonical source URL.
- Mirrors are never exposed as the public source link.
- Discord attachments can supplement poor embeds without replacing source attribution.
- No non-Bluesky-native image post is published without alt text for every image.[web:119]
- Missing data triggers simple in-thread bot requests instead of silent failure.
- Pending submissions can wait indefinitely without harming queue health.
- The system can later gain Matrix ingestion without changing the basic user rules.[web:99]
- The bot behaves like a transparent source-forwarding machine, not a bad-faith content vacuum.
