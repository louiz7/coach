"""add WHOOP OAuth fields to users

Revision ID: 004
Revises: 003
Create Date: 2026-04-23
"""
from alembic import op
import sqlalchemy as sa

revision = '004'
down_revision = '003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('whoop_user_id', sa.String(50), nullable=True))
    op.add_column('users', sa.Column('whoop_access_token', sa.Text(), nullable=True))
    op.add_column('users', sa.Column('whoop_refresh_token', sa.Text(), nullable=True))
    op.add_column('users', sa.Column('whoop_token_expires_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'whoop_token_expires_at')
    op.drop_column('users', 'whoop_refresh_token')
    op.drop_column('users', 'whoop_access_token')
    op.drop_column('users', 'whoop_user_id')
