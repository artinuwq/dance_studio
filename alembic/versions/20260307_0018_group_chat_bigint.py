"""Resize groups.chat_id to BigInteger

Revision ID: 20260307_0018_group_chat_bigint
Revises: 20260305_0017_discounts_v2
Create Date: 2026-03-07
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260307_0018_group_chat_bigint"
down_revision = "20260305_0017_discounts_v2"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column(
        "groups",
        "chat_id",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        existing_nullable=True,
        postgresql_using="chat_id::bigint",
    )


def downgrade():
    op.alter_column(
        "groups",
        "chat_id",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        existing_nullable=True,
        postgresql_using="chat_id::integer",
    )
