"""add user-edit tracking fields to training_plans

Revision ID: 014
Revises: 013
Create Date: 2026-05-05
"""
from alembic import op
import sqlalchemy as sa


revision = "014"
down_revision = "013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "training_plans",
        sa.Column("updated_by_user", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "training_plans",
        sa.Column("user_edited_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("training_plans", "user_edited_at")
    op.drop_column("training_plans", "updated_by_user")
