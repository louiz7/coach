"""widen users.onboarding_state to fit longer state names

Revision ID: 008
Revises: 007
Create Date: 2026-04-29
"""
from alembic import op
import sqlalchemy as sa


revision = '008'
down_revision = '007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        'users',
        'onboarding_state',
        existing_type=sa.String(length=20),
        type_=sa.String(length=50),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        'users',
        'onboarding_state',
        existing_type=sa.String(length=50),
        type_=sa.String(length=20),
        existing_nullable=True,
    )
