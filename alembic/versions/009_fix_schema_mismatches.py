"""Fix model/DB schema mismatches

Revision ID: 009
Revises: 008
Create Date: 2026-05-03

Adds columns that exist in SQLAlchemy models but were missing from the DB:
  - training_plans.plan_json  (JSONB, nullable)
  - progress_entries.category (VARCHAR 30, nullable)
  - progress_entries.notes    (TEXT, nullable)
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = '009'
down_revision = '008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # training_plans: add plan_json JSONB column
    op.add_column('training_plans', sa.Column('plan_json', JSONB, nullable=True))

    # progress_entries: add category and notes
    op.add_column('progress_entries', sa.Column('category', sa.String(30), nullable=True))
    op.add_column('progress_entries', sa.Column('notes', sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column('training_plans', 'plan_json')
    op.drop_column('progress_entries', 'category')
    op.drop_column('progress_entries', 'notes')
