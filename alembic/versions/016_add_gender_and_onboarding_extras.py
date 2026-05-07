"""Add gender column and last_morning_brief_date to users

Revision ID: 016
Revises: 015
Create Date: 2025-01-01
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector

revision = "016"
down_revision = "015"
branch_labels = None
depends_on = None


def _column_exists(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = Inspector.from_engine(bind)
    return column in [c["name"] for c in inspector.get_columns(table)]


def upgrade() -> None:
    # gender — already defined on the model, may not be in DB yet
    if not _column_exists("users", "gender"):
        op.add_column(
            "users",
            sa.Column("gender", sa.String(10), nullable=True),
        )

    # last_morning_brief_date — DB-level dedup guard so Redis loss can't double-send
    if not _column_exists("users", "last_morning_brief_date"):
        op.add_column(
            "users",
            sa.Column("last_morning_brief_date", sa.Date, nullable=True),
        )


def downgrade() -> None:
    op.drop_column("users", "last_morning_brief_date")
    op.drop_column("users", "gender")
