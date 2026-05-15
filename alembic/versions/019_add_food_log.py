"""Add food_log_entries table

Revision ID: 019
Revises: 018
Create Date: 2026-05-15
"""
from alembic import op
import sqlalchemy as sa

revision = "019"
down_revision = "018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "food_log_entries",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("image_url", sa.Text, nullable=True),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("estimated_calories", sa.Integer, nullable=True),
        sa.Column("items_json", sa.Text, nullable=True),
        sa.Column("meal_type", sa.String(20), nullable=True),
        sa.Column(
            "recorded_at",
            sa.DateTime,
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_food_log_entries_user_id",
        "food_log_entries",
        ["user_id"],
    )
    op.create_index(
        "ix_food_log_entries_recorded_at",
        "food_log_entries",
        ["recorded_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_food_log_entries_recorded_at", table_name="food_log_entries")
    op.drop_index("ix_food_log_entries_user_id", table_name="food_log_entries")
    op.drop_table("food_log_entries")
