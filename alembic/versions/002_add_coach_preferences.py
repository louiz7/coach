"""add coach_style, coach_intensity, challenge columns to users

Revision ID: 002
Revises: 001
Create Date: 2026-04-23
"""
from alembic import op
import sqlalchemy as sa

revision = '002'
down_revision = '001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('coach_style', sa.String(30), nullable=True))
    op.add_column('users', sa.Column('coach_intensity', sa.String(20), nullable=True))
    op.add_column('users', sa.Column('challenge', sa.String(30), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'challenge')
    op.drop_column('users', 'coach_intensity')
    op.drop_column('users', 'coach_style')
