"""Source-first Discord -> Bluesky moodboard repost bot.

Milestone 1 scope: Discord ingestion only. A 🦋 reaction creates a tracked
submission; links are canonicalized, attachments downloaded to a persistent
volume, and the bot asks in-thread for any missing source URL / alt text /
graphic classification. No Bluesky publishing yet (that is Milestone 2).

The submission model is intentionally platform-neutral so a Matrix ingestion
adapter can be added later without changing the user-facing rules.
"""

__version__ = "0.1.0"
