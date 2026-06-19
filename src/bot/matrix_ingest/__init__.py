"""Matrix ingestion adapter (RESERVED - Milestone 5).

Must preserve the same user mental model as Discord: a reaction/equivalent means
intent-to-submit, the bot asks for missing source URL / alt text in replies, and
curator roles map onto Matrix power levels. The submission model in `state.py`
and `models.py` is platform-neutral so this adapter drops in without changing the
user-facing rules.
"""
