"""widen research_chunks.category to VARCHAR(80)

Revision ID: 012
Revises: 011
Create Date: 2026-05-04
"""
from alembic import op


revision = '012'
down_revision = '011'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE research_chunks ALTER COLUMN category TYPE VARCHAR(80)")


def downgrade() -> None:
    op.execute("ALTER TABLE research_chunks ALTER COLUMN category TYPE VARCHAR(30)")
