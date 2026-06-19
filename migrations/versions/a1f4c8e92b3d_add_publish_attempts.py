"""add publish_attempts

Revision ID: a1f4c8e92b3d
Revises: 3d15d8253969
Create Date: 2026-06-18 00:00:00.000000

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = 'a1f4c8e92b3d'
down_revision = '3d15d8253969'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "publish_attempts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("submission_id", sa.Integer(), nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("at_uri", sa.Text(), nullable=True),
        sa.Column("at_cid", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["submission_id"], ["submissions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_publish_attempts_submission_id", "publish_attempts", ["submission_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_publish_attempts_submission_id", table_name="publish_attempts")
    op.drop_table("publish_attempts")
