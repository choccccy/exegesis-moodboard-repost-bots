"""add_playlist_item_and_cancel_to_youtube_playlist_add

Revision ID: b9d4a1e7c823
Revises: a3c7e2f9b041
Create Date: 2026-06-23 00:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = 'b9d4a1e7c823'
down_revision = 'a3c7e2f9b041'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('youtube_playlist_add') as batch_op:
        batch_op.add_column(sa.Column('playlist_item_id', sa.String(100), nullable=True))
        batch_op.add_column(sa.Column('cancel_message_id', sa.BigInteger(), nullable=True))
    op.create_index('ix_youtube_playlist_add_cancel_message_id', 'youtube_playlist_add', ['cancel_message_id'])


def downgrade() -> None:
    op.drop_index('ix_youtube_playlist_add_cancel_message_id', 'youtube_playlist_add')
    with op.batch_alter_table('youtube_playlist_add') as batch_op:
        batch_op.drop_column('cancel_message_id')
        batch_op.drop_column('playlist_item_id')
