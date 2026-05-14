"""Add language column to users

Revision ID: 018
Revises: 017
Create Date: 2026-05-14
"""
from alembic import op
import sqlalchemy as sa


revision = "018"
down_revision = "017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE users
        ADD COLUMN IF NOT EXISTS language VARCHAR(5) NOT NULL DEFAULT 'en'
    """)
    # Existing users with +49 numbers → German
    op.execute("""
        UPDATE users SET language = 'de' WHERE phone LIKE '+49%'
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS language")
