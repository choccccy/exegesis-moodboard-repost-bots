"""add_youtube_playlist_add_table

Revision ID: a3c7e2f9b041
Revises: f7b2c9d1e834
Create Date: 2026-06-23 00:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = 'a3c7e2f9b041'
down_revision = 'f7b2c9d1e834'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'youtube_playlist_add',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('board_id', sa.Integer(), sa.ForeignKey('boards.id'), nullable=False),
        sa.Column('source_discord_message_id', sa.BigInteger(), nullable=False),
        sa.Column('video_id', sa.String(20), nullable=False),
        sa.Column('playlist_id', sa.String(100), nullable=False),
        sa.Column('discord_requester_id', sa.BigInteger(), nullable=False),
        sa.Column('added_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('success', sa.Boolean(), nullable=False),
        sa.Column('error_message', sa.Text(), nullable=True),
    )
    op.create_index('ix_youtube_playlist_add_board_id', 'youtube_playlist_add', ['board_id'])
    op.create_index('ix_youtube_playlist_add_source_discord_message_id', 'youtube_playlist_add', ['source_discord_message_id'])
    op.create_index('ix_youtube_playlist_add_video_id', 'youtube_playlist_add', ['video_id'])


def downgrade() -> None:
    op.drop_table('youtube_playlist_add')
