"""
Vector-based semantic memory using pgvector.

- embed(text)                     → OpenAI text-embedding-3-small (1536 dims)
- store_memory(user, content, …)  → INSERT into memory_embeddings
- search_memories(user, query, k) → cosine-similarity top-K relevant memories
"""
from __future__ import annotations

import json
from typing import Optional
from uuid import UUID

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings


EMBED_MODEL = "text-embedding-3-small"
EMBED_DIMS = 1536


# ─── Embedding ───────────────────────────────────────────────────────────────

async def embed(text_to_embed: str) -> Optional[list[float]]:
    """Call OpenAI embeddings API. Returns 1536-dim list or None on failure."""
    if not text_to_embed or not text_to_embed.strip():
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                "https://api.openai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
                json={"model": EMBED_MODEL, "input": text_to_embed[:2000]},
            )
            r.raise_for_status()
            return r.json()["data"][0]["embedding"]
    except Exception as e:
        print(f"[embed ERROR] {e}")
        return None


def _to_pgvector_literal(vec: list[float]) -> str:
    """pgvector accepts text-form '[0.1,0.2,...]' for inserts."""
    return "[" + ",".join(f"{v:.7f}" for v in vec) + "]"


# ─── Storage ─────────────────────────────────────────────────────────────────

async def store_memory(
    user_id: UUID,
    content: str,
    category: str,
    db: AsyncSession,
) -> None:
    """Embed and persist a memory. Categories: workout, recovery, sleep, bodyweight, conversation, goal."""
    vec = await embed(content)
    if vec is None:
        return
    try:
        await db.execute(
            text(
                "INSERT INTO memory_embeddings (user_id, content, embedding, category) "
                "VALUES (:uid, :content, CAST(:emb AS vector), :cat)"
            ),
            {
                "uid": str(user_id),
                "content": content,
                "emb": _to_pgvector_literal(vec),
                "cat": category,
            },
        )
        await db.commit()
    except Exception as e:
        print(f"[store_memory ERROR] {e}")


# ─── Retrieval ───────────────────────────────────────────────────────────────

async def search_memories(
    user_id: UUID,
    query: str,
    db: AsyncSession,
    top_k: int = 5,
    min_similarity: float = 0.3,
) -> list[str]:
    """Cosine-similarity search. Returns list of content strings."""
    vec = await embed(query)
    if vec is None:
        return []

    try:
        # pgvector: 1 - (embedding <=> query) is cosine SIMILARITY (not distance)
        result = await db.execute(
            text(
                "SELECT content, 1 - (embedding <=> CAST(:emb AS vector)) AS sim "
                "FROM memory_embeddings "
                "WHERE user_id = :uid "
                "ORDER BY embedding <=> CAST(:emb AS vector) "
                "LIMIT :k"
            ),
            {
                "uid": str(user_id),
                "emb": _to_pgvector_literal(vec),
                "k": top_k,
            },
        )
        rows = result.fetchall()
        return [row[0] for row in rows if row[1] is not None and row[1] >= min_similarity]
    except Exception as e:
        print(f"[search_memories ERROR] {e}")
        return []


def format_memories_for_prompt(memories: list[str]) -> str:
    if not memories:
        return ""
    lines = ["RELEVANT PAST MEMORIES:"]
    for m in memories:
        lines.append(f"- {m}")
    return "\n".join(lines)
