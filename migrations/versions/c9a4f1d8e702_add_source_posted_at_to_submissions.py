"""add_source_posted_at_to_submissions

Revision ID: c9a4f1d8e702
Revises: 7a2f91bc4e05
Create Date: 2026-06-22 00:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = 'c9a4f1d8e702'
down_revision = '7a2f91bc4e05'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'submissions',
        sa.Column('source_posted_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('submissions', 'source_posted_at')
