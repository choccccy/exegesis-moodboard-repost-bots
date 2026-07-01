"""add_archived_at_to_submission_threads

Revision ID: b9c3d4e5f126
Revises: a8b2d3e4f015
Branch Labels: None
Depends On: None
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = 'b9c3d4e5f126'
down_revision = 'a8b2d3e4f015'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('submission_threads', sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('submission_threads', 'archived_at')
