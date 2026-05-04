# Research papers corpus

Drop curated research papers as `.txt` or `.pdf` files under one of the
topic-based category dirs:

- `assets/papers/mechanisms_hypertrophy/` — mechanisms & general hypertrophy reviews
- `assets/papers/rt_prescription/` — RT prescription & training variables
- `assets/papers/frequency_load_tempo_rest_failure/` — frequency, load, tempo, rest, proximity to failure
- `assets/papers/muscle_protein_synthesis/` — MPS time course & signaling
- `assets/papers/protein_intake/` — dietary protein intake & hypertrophy

## File formats

### `.pdf`
Text is extracted with `pypdf`. Title comes from PDF metadata if present,
otherwise the filename. Source is set to the filename. No special
preparation needed.

### `.txt`
One paper per file. Optional first two lines for metadata:

```
# Effects of Creatine Monohydrate on Strength
SOURCE: Kreider et al., JISSN 2017

Creatine monohydrate is the most extensively studied...
```

If `# Title` (or `TITLE:`) is missing, the filename (without `.txt`) is
used as the title.

## Ingest

After adding/updating papers:

```
docker compose run --rm -T api python -m scripts.ingest_papers
```

By default, papers whose title already exists in `research_chunks` are
skipped. Use `--force` to re-ingest (deletes old chunks for that title
first).

## How retrieval works

When the user asks an `EXERCISE_QUESTION` or `NUTRITION_QUESTION`, the
top 3 most cosine-similar chunks (pgvector) are injected into the
system prompt as a `SCIENTIFIC CONTEXT` block. The model is instructed
to summarize naturally, not cite verbatim.

- Pure `NUTRITION_QUESTION` → search restricted to `protein_intake`.
- Otherwise → search across all categories (cosine similarity decides).
