"""add_source_at_uri_to_submission_links

Revision ID: e5f9a2c7d340
Revises: d4a1c6b8e213
Create Date: 2026-07-17 18:30:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = 'e5f9a2c7d340'
down_revision = 'd4a1c6b8e213'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('submission_links', schema=None) as batch_op:
        batch_op.add_column(sa.Column('source_at_uri', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('submission_links', schema=None) as batch_op:
        batch_op.drop_column('source_at_uri')
