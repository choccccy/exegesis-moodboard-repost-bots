"""add_source_note_and_confirmed_to_submissions

Revision ID: d4a1c6b8e213
Revises: c2f8a1b4d907
Branch Labels: None
Depends On: None
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = 'd4a1c6b8e213'
down_revision = 'c2f8a1b4d907'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'submissions',
        sa.Column('source_note', sa.Text(), nullable=True),
    )
    op.add_column(
        'submissions',
        sa.Column('source_note_confirmed', sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column('submissions', 'source_note_confirmed')
    op.drop_column('submissions', 'source_note')
