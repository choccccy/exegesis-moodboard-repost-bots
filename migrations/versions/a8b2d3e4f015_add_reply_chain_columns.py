"""add reply chain columns

Revision ID: a8b2d3e4f015
Revises: 0d3c9c685ccd
Create Date: 2026-06-26

"""

from alembic import op
import sqlalchemy as sa

revision = "a8b2d3e4f015"
down_revision = "0d3c9c685ccd"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("submissions") as batch_op:
        batch_op.add_column(sa.Column("reply_to_discord_message_id", sa.BigInteger, nullable=True))
        batch_op.create_index("ix_submissions_reply_to_discord_message_id", ["reply_to_discord_message_id"])

    with op.batch_alter_table("publish_attempts") as batch_op:
        batch_op.add_column(sa.Column("bsky_root_uri", sa.Text, nullable=True))
        batch_op.add_column(sa.Column("bsky_root_cid", sa.Text, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("submissions") as batch_op:
        batch_op.drop_index("ix_submissions_reply_to_discord_message_id")
        batch_op.drop_column("reply_to_discord_message_id")

    with op.batch_alter_table("publish_attempts") as batch_op:
        batch_op.drop_column("bsky_root_uri")
        batch_op.drop_column("bsky_root_cid")
