"""add stripe_customer_id and current_period_end to subscriptions

Revision ID: 003
Revises: 002
Create Date: 2026-04-23
"""
from alembic import op
import sqlalchemy as sa

revision = '003'
down_revision = '002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('subscriptions', sa.Column('stripe_customer_id', sa.String(100), nullable=True))
    op.add_column('subscriptions', sa.Column('current_period_end', sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column('subscriptions', 'current_period_end')
    op.drop_column('subscriptions', 'stripe_customer_id')
