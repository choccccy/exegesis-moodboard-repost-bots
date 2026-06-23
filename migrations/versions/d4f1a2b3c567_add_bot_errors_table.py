"""add_bot_errors_table

Revision ID: d4f1a2b3c567
Revises: c9a4f1d8e702
Create Date: 2026-06-23 00:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = 'd4f1a2b3c567'
down_revision = 'c9a4f1d8e702'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'bot_errors',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('source', sa.String(), nullable=False, index=True),
        sa.Column('context', sa.String(), nullable=False),
        sa.Column('traceback', sa.Text(), nullable=False),
        sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('bot_errors')
