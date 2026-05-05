"""Add current_schedule_notes and equipment_access to users

Revision ID: 015
Revises: 014
Create Date: 2025-01-01
"""
from alembic import op
import sqlalchemy as sa

revision = "015"
down_revision = "014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("current_schedule_notes", sa.Text(), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("equipment_access", sa.String(30), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "equipment_access")
    op.drop_column("users", "current_schedule_notes")
