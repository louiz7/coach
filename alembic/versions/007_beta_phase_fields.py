"""beta phase fields: sports_focus, beta_unlocked, widen goal

Revision ID: 007
Revises: 006
Create Date: 2026-04-29
"""
from alembic import op
import sqlalchemy as sa


revision = '007'
down_revision = '006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Free-text list of sports the user wants to improve in
    op.add_column(
        'users',
        sa.Column('sports_focus', sa.Text(), nullable=True),
    )
    # Beta access flag — set True once a user enters the correct beta code
    op.add_column(
        'users',
        sa.Column(
            'beta_unlocked',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('false'),
        ),
    )
    # Widen goal so chat answers (free-text fallback) don't truncate
    op.alter_column(
        'users',
        'goal',
        existing_type=sa.String(length=50),
        type_=sa.Text(),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        'users',
        'goal',
        existing_type=sa.Text(),
        type_=sa.String(length=50),
        existing_nullable=True,
    )
    op.drop_column('users', 'beta_unlocked')
    op.drop_column('users', 'sports_focus')
