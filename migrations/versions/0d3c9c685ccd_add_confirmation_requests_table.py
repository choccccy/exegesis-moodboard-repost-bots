"""add_confirmation_requests_table

Revision ID: 0d3c9c685ccd
Revises: c4e8f2a1d693
Branch Labels: None
Depends On: None
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0d3c9c685ccd'
down_revision = 'c4e8f2a1d693'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'confirmation_requests',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('submission_id', sa.Integer(), nullable=False),
        sa.Column('bot_message_id', sa.BigInteger(), nullable=False),
        sa.Column('prompted_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('confirmed_by', sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(['submission_id'], ['submissions.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('submission_id'),
        sa.UniqueConstraint('bot_message_id'),
    )
    op.create_index('ix_confirmation_requests_submission_id', 'confirmation_requests', ['submission_id'])
    op.create_index('ix_confirmation_requests_bot_message_id', 'confirmation_requests', ['bot_message_id'])


def downgrade() -> None:
    op.drop_index('ix_confirmation_requests_bot_message_id', table_name='confirmation_requests')
    op.drop_index('ix_confirmation_requests_submission_id', table_name='confirmation_requests')
    op.drop_table('confirmation_requests')
