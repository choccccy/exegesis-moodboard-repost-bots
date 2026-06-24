"""add_playlist_opt_out_to_submissions

Revision ID: c1e5f3a9d024
Revises: b9d4a1e7c823
Create Date: 2026-06-23 00:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = 'c1e5f3a9d024'
down_revision = 'b9d4a1e7c823'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('submissions') as batch_op:
        batch_op.add_column(sa.Column('playlist_skipped', sa.Boolean(), nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('playlist_opt_out_message_id', sa.BigInteger(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('submissions') as batch_op:
        batch_op.drop_column('playlist_opt_out_message_id')
        batch_op.drop_column('playlist_skipped')
