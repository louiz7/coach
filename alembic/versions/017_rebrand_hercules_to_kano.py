"""Rebrand: migrate users.project from 'hercules' to 'kano'

Revision ID: 017
Revises: 016
Create Date: 2026-05-09
"""
from alembic import op


revision = "017"
down_revision = "016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # All existing users were tagged as 'hercules' under the old branding.
    # twinnn has been decommissioned, so we can safely promote everyone to 'kano'.
    op.execute("UPDATE users SET project = 'kano' WHERE project = 'hercules'")


def downgrade() -> None:
    op.execute("UPDATE users SET project = 'hercules' WHERE project = 'kano'")
