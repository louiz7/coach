"""add linq_message_id to messages

Revision ID: 010
Revises: 009
Create Date: 2025-05-03
"""
from alembic import op
import sqlalchemy as sa

revision = '010'
down_revision = '009'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'messages',
        sa.Column('linq_message_id', sa.String(100), nullable=True)
    )


def downgrade():
    op.drop_column('messages', 'linq_message_id')
