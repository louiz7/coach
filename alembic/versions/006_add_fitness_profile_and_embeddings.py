"""add fitness_profiles and memory_embeddings (pgvector)

Revision ID: 006
Revises: 005
Create Date: 2026-04-26
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '006'
down_revision = '005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Enable pgvector extension (idempotent)
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # fitness_profiles — one row per user, structured JSONB
    op.create_table(
        'fitness_profiles',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text('gen_random_uuid()')),
        sa.Column('user_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('users.id', ondelete='CASCADE'),
                  nullable=False, unique=True),
        sa.Column('profile', postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column('updated_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_fitness_profiles_user_id', 'fitness_profiles', ['user_id'])

    # memory_embeddings — searchable raw memories with pgvector embeddings
    op.execute("""
        CREATE TABLE memory_embeddings (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            content TEXT NOT NULL,
            embedding vector(1536) NOT NULL,
            category VARCHAR(50) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.create_index('ix_memory_embeddings_user_id', 'memory_embeddings', ['user_id'])
    op.create_index('ix_memory_embeddings_category', 'memory_embeddings', ['category'])
    # IVFFLAT index for fast cosine similarity (use after data inserted, but cheap to create now)
    op.execute(
        "CREATE INDEX ix_memory_embeddings_embedding "
        "ON memory_embeddings USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_memory_embeddings_embedding")
    op.drop_index('ix_memory_embeddings_category', table_name='memory_embeddings')
    op.drop_index('ix_memory_embeddings_user_id', table_name='memory_embeddings')
    op.execute("DROP TABLE IF EXISTS memory_embeddings")
    op.drop_index('ix_fitness_profiles_user_id', table_name='fitness_profiles')
    op.drop_table('fitness_profiles')
