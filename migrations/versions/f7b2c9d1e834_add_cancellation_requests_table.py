"""add_cancellation_requests_table

Revision ID: f7b2c9d1e834
Revises: d4f1a2b3c567
Create Date: 2026-06-23 00:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = 'f7b2c9d1e834'
down_revision = 'd4f1a2b3c567'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cancellation_requests',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('submission_id', sa.Integer(), sa.ForeignKey('submissions.id'), nullable=False, unique=True),
        sa.Column('bot_message_id', sa.BigInteger(), nullable=False, unique=True),
        sa.Column('prompted_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_cancellation_requests_submission_id', 'cancellation_requests', ['submission_id'])
    op.create_index('ix_cancellation_requests_bot_message_id', 'cancellation_requests', ['bot_message_id'])


def downgrade() -> None:
    op.drop_table('cancellation_requests')
