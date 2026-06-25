"""add_confirmation_requests_table

Revision ID: d5a9c3f1b827
Revises: c4e8f2a1d693
Branch Labels: None
Depends On: None
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = 'd5a9c3f1b827'
down_revision = 'c4e8f2a1d693'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'confirmation_requests',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('submission_id', sa.Integer(), sa.ForeignKey('submissions.id'), nullable=False),
        sa.Column('bot_message_id', sa.BigInteger(), nullable=False),
        sa.Column('prompted_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('confirmed_by', sa.BigInteger(), nullable=True),
    )
    op.create_index('ix_confirmation_requests_submission_id', 'confirmation_requests', ['submission_id'], unique=True)
    op.create_index('ix_confirmation_requests_bot_message_id', 'confirmation_requests', ['bot_message_id'], unique=True)


def downgrade() -> None:
    op.drop_table('confirmation_requests')
