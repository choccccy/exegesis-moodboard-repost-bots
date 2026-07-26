"""Surface-agnostic curation core: the ports and vocabulary the curation flow speaks,
independent of any chat platform (issue #50).

Nothing in this package may import `discord` (or any other surface SDK) - that rule is
enforced by tests/test_curation_boundary.py. A per-surface *adapter* (Discord:
`bot.discord_ingest`; a future `bot.matrix_ingest`) implements the `Surface` port and
translates native events into the normalized `InteractionEvent` / `InboundMessage`
types, then performs the handlers' `HandlerOutcome`s.

Modules:
  components — abstract UI descriptors (buttons/selects/modals/previews)
  surface    — the outbound `Surface` port (+ `NullSurface`)
  events     — normalized inbound `InteractionEvent`
  outcomes   — `HandlerOutcome` variants a handler returns
  types      — normalized inbound message vocabulary (`InboundMessage`, ...)
"""
