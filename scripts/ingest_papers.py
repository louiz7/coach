"""
Ingest research papers from assets/papers/<category>/*.{txt,pdf} into research_chunks.

Categories (topic-based, see assets/papers/README.md):
  - mechanisms_hypertrophy
  - rt_prescription
  - frequency_load_tempo_rest_failure
  - muscle_protein_synthesis
  - protein_intake

Each file = one paper.
- For .txt: filename (without ext) is the title unless the first line is `# Title`
  or `TITLE:`. Optional `SOURCE:` line on line 2 sets the source field.
- For .pdf: text is extracted with pypdf. Title = PDF metadata title if present,
  otherwise the filename. Source = filename.

Run inside the api container:
  docker compose run --rm -T api python -m scripts.ingest_papers
or locally:
  python -m scripts.ingest_papers

Re-running: by default the script SKIPS papers whose title already exists
in research_chunks. Pass --force to re-ingest (will delete existing chunks
for that title first).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from sqlalchemy import text

from app.database import async_session
from app.services.research_rag import embed_and_store_paper


VALID_CATEGORIES = {
    "mechanisms_hypertrophy",
    "rt_prescription",
    "frequency_load_tempo_rest_failure",
    "muscle_protein_synthesis",
    "protein_intake",
}


def _parse_txt(path: Path) -> tuple[str, str | None, str]:
    """Returns (title, source, body)."""
    raw = path.read_text(encoding="utf-8", errors="replace")
    lines = raw.splitlines()
    title = path.stem
    source: str | None = None
    body_start = 0

    if lines:
        first = lines[0].strip()
        if first.startswith("# "):
            title = first[2:].strip()
            body_start = 1
        elif first.upper().startswith("TITLE:"):
            title = first.split(":", 1)[1].strip()
            body_start = 1

    if len(lines) > body_start:
        second = lines[body_start].strip()
        if second.upper().startswith("SOURCE:"):
            source = second.split(":", 1)[1].strip()
            body_start += 1

    body = "\n".join(lines[body_start:]).strip()
    return title, source, body


def _parse_pdf(path: Path) -> tuple[str, str | None, str]:
    """Extract text from a PDF using pypdf. Returns (title, source, body)."""
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise SystemExit(
            "pypdf is required to ingest PDFs. Install it: pip install pypdf"
        ) from e

    reader = PdfReader(str(path))
    meta_title = None
    try:
        if reader.metadata and reader.metadata.title:
            meta_title = str(reader.metadata.title).strip() or None
    except Exception:
        meta_title = None

    title = meta_title or path.stem
    source = path.name

    parts: list[str] = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception as e:
            print(f"[ingest] pdf page extract error in {path.name}: {e}")
    body = "\n".join(parts).strip()
    return title, source, body


def _parse_paper(path: Path) -> tuple[str, str | None, str]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _parse_pdf(path)
    return _parse_txt(path)


async def _title_exists(db, title: str) -> bool:
    r = await db.execute(
        text("SELECT 1 FROM research_chunks WHERE title = :t LIMIT 1"),
        {"t": title},
    )
    return r.first() is not None


async def _delete_title(db, title: str) -> None:
    await db.execute(
        text("DELETE FROM research_chunks WHERE title = :t"),
        {"t": title},
    )
    await db.commit()


async def ingest(papers_dir: Path, force: bool) -> None:
    if not papers_dir.exists():
        print(f"[ingest] dir not found: {papers_dir}")
        return

    total_papers = 0
    total_chunks = 0
    for category_dir in sorted(papers_dir.iterdir()):
        if not category_dir.is_dir():
            continue
        category = category_dir.name.lower()
        if category not in VALID_CATEGORIES:
            print(f"[ingest] skipping unknown category dir: {category}")
            continue

        files = sorted(
            [p for p in category_dir.iterdir()
             if p.is_file() and p.suffix.lower() in {".txt", ".pdf"}]
        )
        if not files:
            continue

        async with async_session() as db:
            for path in files:
                try:
                    title, source, body = _parse_paper(path)
                except Exception as e:
                    print(f"[ingest] parse error {path.name}: {e}")
                    continue

                if not body:
                    print(f"[ingest] empty body, skip: {path}")
                    continue

                if await _title_exists(db, title):
                    if not force:
                        print(f"[ingest] exists, skip: [{category}] {title}")
                        continue
                    print(f"[ingest] force re-ingest, deleting old chunks: {title}")
                    await _delete_title(db, title)

                stored = await embed_and_store_paper(
                    title=title,
                    source=source,
                    category=category,
                    full_text=body,
                    db=db,
                )
                total_papers += 1
                total_chunks += stored
                print(f"[ingest] [{category}] {title} → {stored} chunks")

    print(f"[ingest] done. papers={total_papers} chunks={total_chunks}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--dir",
        default=os.environ.get("PAPERS_DIR", "assets/papers"),
        help="root directory containing <category>/*.{txt,pdf}",
    )
    p.add_argument("--force", action="store_true", help="re-ingest existing titles")
    args = p.parse_args()

    asyncio.run(ingest(Path(args.dir), args.force))


if __name__ == "__main__":
    sys.exit(main() or 0)
