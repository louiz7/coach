"""add research_chunks for scientific RAG

Revision ID: 011
Revises: 010
Create Date: 2025-05-04
"""
from alembic import op


revision = '011'
down_revision = '010'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # pgvector already enabled in 006, but be idempotent
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # research_chunks — global (not per-user) scientific knowledge corpus
    # category: 'exercise' | 'nutrition' | 'recovery' | 'general'
    op.execute("""
        CREATE TABLE research_chunks (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            title TEXT NOT NULL,
            source TEXT,
            category VARCHAR(30) NOT NULL,
            chunk_index INT NOT NULL,
            chunk_text TEXT NOT NULL,
            embedding vector(1536) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.create_index('ix_research_chunks_category', 'research_chunks', ['category'])
    op.execute(
        "CREATE INDEX ix_research_chunks_embedding "
        "ON research_chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_research_chunks_embedding")
    op.drop_index('ix_research_chunks_category', table_name='research_chunks')
    op.execute("DROP TABLE IF EXISTS research_chunks")
