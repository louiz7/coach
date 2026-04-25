"""add WHOOP biometric cache fields and timezone to users

Revision ID: 005
Revises: 004
Create Date: 2026-04-25
"""
from alembic import op
import sqlalchemy as sa

revision = '005'
down_revision = '004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('last_recovery_score', sa.Integer(), nullable=True))
    op.add_column('users', sa.Column('last_hrv', sa.Float(), nullable=True))
    op.add_column('users', sa.Column('last_sleep_performance', sa.Integer(), nullable=True))
    op.add_column('users', sa.Column('timezone', sa.String(50), nullable=True, server_default='Europe/Berlin'))


def downgrade() -> None:
    op.drop_column('users', 'timezone')
    op.drop_column('users', 'last_sleep_performance')
    op.drop_column('users', 'last_hrv')
    op.drop_column('users', 'last_recovery_score')
