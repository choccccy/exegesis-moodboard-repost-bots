"""add_metadata_requests

Revision ID: 7a2f91bc4e05
Revises: 313cf88a0630
Create Date: 2026-06-22 00:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '7a2f91bc4e05'
down_revision = '313cf88a0630'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'metadata_requests',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('submission_id', sa.Integer(), sa.ForeignKey('submissions.id'), nullable=False),
        sa.Column('bot_message_id', sa.BigInteger(), nullable=False, unique=True),
        sa.Column('prompted_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('answered_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('answer', sa.Text(), nullable=True),
        sa.Column('answered_by', sa.BigInteger(), nullable=True),
    )
    op.create_index('ix_metadata_requests_submission_id', 'metadata_requests', ['submission_id'])
    op.create_index('ix_metadata_requests_bot_message_id', 'metadata_requests', ['bot_message_id'], unique=True)


def downgrade() -> None:
    op.drop_index('ix_metadata_requests_bot_message_id', table_name='metadata_requests')
    op.drop_index('ix_metadata_requests_submission_id', table_name='metadata_requests')
    op.drop_table('metadata_requests')
