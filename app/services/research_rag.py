"""
Scientific RAG over a curated research-paper corpus (global, not per-user).

- chunk_text_into_words(text, words_per_chunk=400)
- embed_and_store_paper(title, source, category, full_text, db)
- search_research(query, db, category_hint=None, top_k=3)
- format_research_for_prompt(chunks)

Mirrors `app/services/memory_search.py` patterns:
  * httpx call to /v1/embeddings (text-embedding-3-small, 1536 dims)
  * raw SQL via text() with CAST(:emb AS vector)
  * pgvector cosine: 1 - (embedding <=> q) is similarity
"""
from __future__ import annotations

import re
from typing import Optional

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings


EMBED_MODEL = "text-embedding-3-small"
EMBED_DIMS = 1536
DEFAULT_WORDS_PER_CHUNK = 400


# ─── Embedding (same pattern as memory_search.embed) ─────────────────────────

async def embed(text_to_embed: str) -> Optional[list[float]]:
    if not text_to_embed or not text_to_embed.strip():
        return None
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                "https://api.openai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
                # text-embedding-3-small accepts up to 8191 tokens; 400 words ≈ 600 tokens — safe
                json={"model": EMBED_MODEL, "input": text_to_embed[:8000]},
            )
            r.raise_for_status()
            return r.json()["data"][0]["embedding"]
    except Exception as e:
        print(f"[research_rag.embed ERROR] {e}")
        return None


def _to_pgvector_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{v:.7f}" for v in vec) + "]"


# ─── Chunking ────────────────────────────────────────────────────────────────

def _normalize_whitespace(s: str) -> str:
    # collapse newlines and runs of whitespace; keep paragraph hint as a space
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def chunk_text_into_words(
    full_text: str,
    words_per_chunk: int = DEFAULT_WORDS_PER_CHUNK,
) -> list[str]:
    """Split into ~N-word chunks at word boundaries."""
    cleaned = _normalize_whitespace(full_text)
    if not cleaned:
        return []
    words = cleaned.split(" ")
    chunks: list[str] = []
    for i in range(0, len(words), words_per_chunk):
        chunk = " ".join(words[i : i + words_per_chunk]).strip()
        if chunk:
            chunks.append(chunk)
    return chunks


# ─── Storage ─────────────────────────────────────────────────────────────────

async def embed_and_store_paper(
    title: str,
    source: Optional[str],
    category: str,
    full_text: str,
    db: AsyncSession,
    words_per_chunk: int = DEFAULT_WORDS_PER_CHUNK,
) -> int:
    """Chunk → embed → insert. Returns number of chunks stored."""
    chunks = chunk_text_into_words(full_text, words_per_chunk=words_per_chunk)
    stored = 0
    for idx, chunk in enumerate(chunks):
        vec = await embed(chunk)
        if vec is None:
            continue
        try:
            await db.execute(
                text(
                    "INSERT INTO research_chunks "
                    "(title, source, category, chunk_index, chunk_text, embedding) "
                    "VALUES (:title, :source, :cat, :idx, :chunk, CAST(:emb AS vector))"
                ),
                {
                    "title": title,
                    "source": source,
                    "cat": category,
                    "idx": idx,
                    "chunk": chunk,
                    "emb": _to_pgvector_literal(vec),
                },
            )
            stored += 1
        except Exception as e:
            print(f"[embed_and_store_paper ERROR idx={idx}] {e}")
    await db.commit()
    return stored


# ─── Retrieval ───────────────────────────────────────────────────────────────

async def search_research(
    query: str,
    db: AsyncSession,
    category_hint: Optional[str] = None,
    top_k: int = 3,
    min_similarity: float = 0.25,
) -> list[dict]:
    """Return list of {title, source, chunk_text, sim} dicts."""
    vec = await embed(query)
    if vec is None:
        return []

    params = {
        "emb": _to_pgvector_literal(vec),
        "k": top_k,
    }
    where_clause = ""
    if category_hint:
        where_clause = "WHERE category = :cat "
        params["cat"] = category_hint

    try:
        result = await db.execute(
            text(
                "SELECT title, source, chunk_text, "
                "1 - (embedding <=> CAST(:emb AS vector)) AS sim "
                "FROM research_chunks "
                f"{where_clause}"
                "ORDER BY embedding <=> CAST(:emb AS vector) "
                "LIMIT :k"
            ),
            params,
        )
        rows = result.fetchall()
        out = []
        for row in rows:
            sim = row[3]
            if sim is None or sim < min_similarity:
                continue
            out.append({
                "title": row[0],
                "source": row[1],
                "chunk_text": row[2],
                "sim": float(sim),
            })
        return out
    except Exception as e:
        print(f"[search_research ERROR] {e}")
        return []


def format_research_for_prompt(chunks: list[dict]) -> str:
    """Compact block. Keep small to control token cost."""
    if not chunks:
        return ""
    lines = ["SCIENTIFIC CONTEXT (use these findings; do not cite verbatim, summarize naturally):"]
    for c in chunks:
        src = f" — {c['source']}" if c.get("source") else ""
        lines.append(f"- [{c['title']}{src}] {c['chunk_text']}")
    return "\n".join(lines)
