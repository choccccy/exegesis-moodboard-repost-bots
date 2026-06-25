"""add_supplemental_link_requests_table

Revision ID: b3d7e1f2a940
Revises: a9c2e4f6b018
Branch Labels: None
Depends On: None
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = 'b3d7e1f2a940'
down_revision = 'a9c2e4f6b018'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'supplemental_link_requests',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('submission_id', sa.Integer(), sa.ForeignKey('submissions.id'), nullable=False),
        sa.Column('bot_message_id', sa.BigInteger(), nullable=False, unique=True),
        sa.Column('prompted_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('answered_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('answer', sa.Text(), nullable=True),
        sa.Column('answered_by', sa.BigInteger(), nullable=True),
    )
    op.create_index('ix_supplemental_link_requests_submission_id', 'supplemental_link_requests', ['submission_id'])
    op.create_index('ix_supplemental_link_requests_bot_message_id', 'supplemental_link_requests', ['bot_message_id'])


def downgrade() -> None:
    op.drop_table('supplemental_link_requests')
