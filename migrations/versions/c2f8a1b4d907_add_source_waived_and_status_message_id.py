"""add_source_waived_and_status_message_id_to_submissions

Revision ID: c2f8a1b4d907
Revises: b9c3d4e5f126
Branch Labels: None
Depends On: None
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = 'c2f8a1b4d907'
down_revision = 'b9c3d4e5f126'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'submissions',
        sa.Column('source_waived', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        'submissions',
        sa.Column('status_message_id', sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('submissions', 'status_message_id')
    op.drop_column('submissions', 'source_waived')
