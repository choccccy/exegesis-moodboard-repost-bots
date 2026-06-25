"""add_is_video_to_attachments

Revision ID: c4e8f2a1d693
Revises: b3d7e1f2a940
Branch Labels: None
Depends On: None
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = 'c4e8f2a1d693'
down_revision = 'b3d7e1f2a940'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('attachments', sa.Column('is_video', sa.Boolean(), nullable=False, server_default='0'))


def downgrade() -> None:
    op.drop_column('attachments', 'is_video')
