# Move network & Discord I/O out of the global DB lock

**Type:** Refactor / responsiveness
**Status:** In progress - Step 1 test harness landed; refactor not yet started
**Risk:** Medium (touches every ingest hot path; correctness rests on a test net)

## Progress

- [x] **Step 1 harness** (`tests/conftest.py`): `db_lock_held` ContextVar,
  `InstrumentedLock`, `record_if_under_lock`, and the `lock_probe` fixture
  (real `session_scope` + real lock, instrumented).
- [x] **Invariant tests** (`tests/test_lock_invariants.py`), both
  `xfail(strict=True)` today - they record I/O performed under the lock and
  will flip to passing (turning the suite red until the marker is removed) once
  each path is refactored:
  - publish path - detects `login`, `create_record`, `like` under the lock.
  - `handle_reaction` - detects `http.get`, `create_thread`, and every
    `thread.send` under the lock.
- [x] **Step 0 characterization audit**: the three flagged thin spots turned out
  to be well covered already - duplicate branch (`test_handle_reaction.py`
  `test_duplicate_of_*`), recompute emission + sequential idempotency
  (`test_recompute.py` / `test_integration_recompute.py`, incl.
  `test_recompute_reentrant_does_not_duplicate_open_requests`), and PublishAttempt
  (`test_attempt_publish.py`). No new golden tests needed; the net is strong.
- [x] **Step 2 investigated - approach changed** (see revised section below). A
  probabilistic concurrent-double-post guard was prototyped and **removed**: it
  could not be made to bite (details below), so it would have been false
  confidence. The concurrency property will be guarded deterministically against
  the per-submission lock, as part of the refactor step that introduces it.
- [x] **Refactor: publish path** (done). `publish_queued_submission` is now
  self-managing - `(settings, submission_id, destination)`, opens its own short
  DB scopes in beats (load/decide -> network publish with lock released -> record
  result -> Discord notice). The scheduler's `_fire_board` / `_fire_all_boards`
  no longer wrap the publish in a `session_scope`, and resolve the thread channel
  (`bot.fetch_channel`) with the lock released too. `_attempt_publish` was folded
  in and removed. The publish invariant test now passes (xfail removed). Behavior
  preserved: full suite green.
- [ ] **Refactor: reaction path** - per-submission lock across all
  `recompute_and_request` callers, `_ensure_thread`, `handle_reaction` ingest.
- [ ] Flip the `handle_reaction` invariant test once that lands.

## Problem

Under load - e.g. a batch of messages butterflied at once - the bot acknowledges
interactions immediately but takes several seconds to *action* them. Queue
confirmations, alt edits, etc. show as received (spinner clears) but the actual
work lands "a little while" later.

The bot is otherwise working correctly. This is purely a responsiveness/latency
defect, not a data-integrity one. We want to fix the latency **without** changing
any observable behavior.

## Root cause

`session_scope()` holds a single process-wide `asyncio.Lock` (`_db_lock`) for its
**entire duration**, serializing *all* DB access - reads included
(`src/bot/db.py:20-24`, `:52-53`). SQLite is single-writer, so the lock is
legitimate. The bug is that **slow I/O runs while the lock is held.**

Two hot paths hold the lock across seconds of network / Discord API work:

### 1. `handle_reaction` (the 🦋 path)

Runs entirely inside `session_scope` (`client.py:311-312`). While holding the
lock it performs:

- `_ingest_content` + `_resolve_links` - outbound HTTP for metadata/thumbnails
  (`service.py:346-347`)
- `_ensure_thread` -> `message.channel.create_thread(...)`, a **rate-limited**
  Discord call wrapped in `asyncio.timeout(15)` precisely because discord.py
  would otherwise sleep up to 5 minutes on a 429 (`service.py:426-434`). Under a
  butterfly storm, thread creation gets 429'd, so **each submission can hold the
  lock for up to 15s**.
- `_post_thread_anchor`, duplicate notices, `recompute_and_request` - more
  Discord sends.

### 2. The publish path

`publish_queued_submission` -> `_attempt_publish` -> `publish_submission` all run
inside the scheduler's `session_scope` (`scheduler/__init__.py:247`,
`service.py:2902`). The full Bluesky conversation (login, blob uploads, record
creation, likes, reply thread) holds the lock throughout - often longer than the
login retry we recently added.

### The symptom, precisely

Button clicks `defer()` **before** taking the lock (`client.py:247-248`; the
comment at `:222-223` explains this keeps us inside Discord's 3s ack window).
So the ack is instant, but `handle_confirm_button` et al. then queue behind
`_db_lock`. During a storm, N `handle_reaction` coroutines serialize through the
one lock, each holding it across a rate-limited `create_thread`. A Queue click
lands at the back of that queue -> acked now, actioned much later.

## The hidden second responsibility (critical)

The global lock is doing **two** jobs today:

1. SQLite single-writer serialization (its stated purpose).
2. **Accidental per-submission mutual exclusion** for read-modify-write
   idempotency.

`recompute_and_request` is a read-modify-write: it checks "is there already an
open CancellationRequest / SupplementalImageRequest / ..." then sends a Discord
message and inserts a row keyed to that message id (`service.py:2634-2680`).
It is called from ~13 sites. **Only the reaction path (`service.py:385`) holds
the per-message lock** (`_message_processing_locks`, `service.py:318`). Every
button handler (`:673, :706, :746, ...`) relies *only* on the global DB lock to
stop two concurrent handlers double-posting requests or double-transitioning
state for the same submission.

**Consequence for the refactor:** naively releasing `_db_lock` during I/O
removes the only thing serializing those read-modify-writes. We must replace job
(2) with an explicit per-submission lock covering *all* handlers before, or in
the same change as, narrowing the DB lock. This is the main correctness hazard.

## Design goal

> No network call and no Discord API call is ever awaited while `_db_lock` is
> held.

The lock should wrap only in-memory DB reads/writes. All I/O happens with the
lock released, guarded (where it mutates shared submission state) by a
per-submission `asyncio.Lock`.

## Proposed shape

Three transaction "beats" per hot path, I/O in between:

1. **Read/prepare** (lock): load or create the submission row, flush to assign
   `submission.id`, snapshot everything downstream needs into a plain dataclass.
   Release.
2. **I/O** (no DB lock; under per-submission lock): HTTP resolution, thread
   creation, anchor/request/notice sends, or the full Bluesky publish.
3. **Persist** (lock): write back results (thread_id, `SubmissionThread` mapping,
   resolved link metadata, request-tracking rows, state transition, PublishAttempt).
   Release.

### `recompute_and_request` is the hard case

Its sends and inserts are interleaved: each row stores a Discord message id that
only exists *after* the send. So it can't be a clean read/IO/write split.
Restructure each request-type block as: **(a)** read-check under lock whether the
request is already open, **(b)** if not, send outside the lock, **(c)** insert
the tracking row in a short transaction. The open-check in (a) and insert in (c)
must be protected by the per-submission lock so two concurrent recomputes can't
both pass the check and double-post. Prefer moving the whole
`recompute_and_request` critical section under the per-submission lock and making
the DB touches short, rather than trying to eliminate interleaving.

### `_ensure_thread`

The `create_thread` call and anchor post move outside the DB lock. The
`SubmissionThread` mapping read (dedupe existing thread) and write move into
beats 1 and 3. Keep the per-message lock so two 🦋 reactions on the same message
can't create two threads.

### Publish path

Open a short session to load the submission + links + attachments into a
snapshot (this largely exists as `_snapshot`), release, run `publish_submission`
with no lock held, then a short session to record the `PublishAttempt` and state
transition. The scheduler already picks one submission per tick, so
cross-submission contention here is low; the win is not blocking interactive
handlers during a multi-second publish.

## Testing strategy (pin behavior through the refactor)

This is the crux: the bot works today, so the test net must **lock in current
observable behavior first**, then let us move code underneath it freely.

### What "observable behavior" means here

For each handler, the contract is:
1. **Resulting DB state** - submission state, rows created/updated.
2. **Discord side effects, in order** - messages sent, in-place edits, reactions,
   archive calls, which views/buttons are attached.
3. **Idempotency** - running the same handler twice (or two racing handlers on
   one submission) does not double-post or double-transition.

The existing suite already captures (1) and (2) for many flows via `MockDest`
(records `.sent` / `.edits`) and `bound_session_scope` (conftest.py:66-122).
58 test files exist, including `test_handle_reaction.py`, `test_ensure_thread.py`,
`test_button_handlers.py`, `test_e2e_flows.py`, `test_e2e_publish.py`, and the
`test_integration_*.py` set.

### Step 0 - Characterization pass (before touching src)

1. Run the full suite; record it green as the baseline.
2. Audit coverage of each hot path against the 3-part contract above. For any
   gap, **add a characterization test now** (against current code) asserting the
   full observable output - the exact `MockDest.sent` sequence, the DB rows, the
   final state. Known thin spots to check: the duplicate-detection branch in
   `handle_reaction` (`service.py:360-383`), the full `recompute_and_request`
   request-emission sequence, and the publish->`PublishAttempt` recording.
3. These become golden tests. The refactor is "done" when they are all still
   green **without being edited**. Editing a characterization test during the
   refactor is a signal that behavior changed - review, don't rubber-stamp.

### Step 1 - New invariant test: no I/O under the lock

This is what makes the refactor *stick*. Turn the design goal into an enforced,
permanent invariant so any future regression fails loudly.

- Add a test-only instrumented lock: wrap `_db_lock` acquire/release to set a
  `ContextVar` `_lock_held = True/False`.
- Patch the I/O boundaries to assert `_lock_held is False` when entered:
  `channel.create_thread`, the shared `httpx.AsyncClient` request path, and the
  atproto client's `login` / `create_record` / `upload_blob`.
- Drive each hot path (a real `handle_reaction`, a real publish) through the
  `global_engine` fixture (conftest.py:92-108, which uses the *real*
  `session_scope` with a real lock, unlike `bound_session_scope`).
- Pre-refactor this test **fails** (proves it detects the defect); post-refactor
  it passes and guards forever.

### Step 2 - Concurrency / contention tests (revised after investigation)

**What we tried and rejected.** The plan was a probabilistic idempotency test:
fire two `recompute_and_request` concurrently for the same submission and assert
exactly one cancel-request row. It does not work as a guard. Empirically, with
the global lock removed (`_db_lock = None`) - *and even with the two tasks forced
by a barrier to both pass the `has_cancel` read before either commits* - the
harness still produced exactly one row. aiosqlite serializes the writes underneath
the app, so the TOCTOU window never manifests as a second row in tests. A test
that passes whether or not the protection exists is worse than no test.

This also tempers the real-world risk: an accidental missing per-submission lock
may not actually double-post under SQLite in practice. We still add the lock -
correctness of the check-then-insert should not rest on incidental storage-engine
write serialization - but it lowers the severity if we get it slightly wrong.

**The deterministic replacement** (write it *with* the refactor step that adds
the per-submission lock, not before - it needs the lock to exist):

- **Per-submission mutual exclusion (structural, deterministic):** acquire the
  per-submission lock for submission X inside a task, park it there on an
  `asyncio.Event`; assert a second acquisition for **X** blocks while an
  acquisition for a **different** submission Y proceeds immediately. This asserts
  the mechanism directly and cannot flake - no attempt to reproduce a race.
- **Lock released during I/O (behavioral, deterministic):** this one *can* be
  written now if we want a behavioral complement - park a hot path's I/O on an
  `asyncio.Event` and assert an independent `session_scope` write is *not* starved.
  But note it is logically entailed by the Step 1 invariant (no I/O under the lock
  => the lock is never held across a parked I/O => nothing is starved), so it is
  optional. Skipped for now to avoid a redundant, signature-coupled test; revisit
  if a reviewer wants the behavioral proof spelled out.

**Thread dedupe race** is already protected by the per-message lock
(`_message_processing_locks`, `service.py:318`), which is independent of the DB
lock and survives the refactor untouched - no new test needed there.

### Step 3 - Optional lock-hold-time assertion

A lighter guard: instrument the lock to record max contiguous hold duration
across a simulated storm, assert it stays under a small bound (e.g. < 100ms) with
`create_thread`/HTTP stubbed to a fixed delay. Cheaper than Step 1 but coarser;
Step 1 is the stronger invariant. Include only if it earns its keep.

### Workflow

Test-first, path-by-path: land Step 0 characterization + Step 1 invariant test
for a path (both against current code, invariant test xfail'd), then refactor
that path until the invariant test flips to pass and characterization stays
green. Ship one hot path at a time (suggested order below) rather than one big
diff - each is independently valuable and independently revertible.

## Sequencing

1. Test net: Step 0 characterization gaps + Step 1 invariant harness (xfail).
2. Introduce/extend the per-submission lock to cover **all** `recompute_and_request`
   callers (behavior-preserving on its own; the global lock still backs it).
3. Refactor `_ensure_thread` (thread creation + anchor out of the lock).
4. Refactor `handle_reaction` ingest/resolve out of the lock.
5. Refactor the publish path.
6. Flip the invariant test from xfail to must-pass; delete any now-dead lock
   assumptions.

## Non-goals / explicitly out of scope

- Replacing SQLite or introducing a real connection pool / WAL concurrency. The
  single-writer lock stays; we only stop holding it across I/O.
- Changing any user-visible message text, ordering, or flow.
- The Bluesky login retry (already shipped in 1.11.1).

## Risks

- **Lost serialization** (primary): covered by the per-submission lock + Step 2
  idempotency tests. Do not narrow the DB lock before the per-submission lock is
  in place and tested.
- **Stale reads across beats:** a submission could change between beat 1 and
  beat 3. Re-read and re-validate in beat 3 rather than trusting the beat-1
  snapshot for write decisions; characterization tests for the duplicate/cancel
  branches guard this.
- **Partial failure between beats:** I/O succeeds but the persist transaction
  fails (or vice versa). Define the recovery per path - e.g. a thread created but
  not persisted should be recoverable by the existing periodic rescan. Note each
  path's failure mode explicitly during implementation.
