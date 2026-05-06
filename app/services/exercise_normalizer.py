"""Post-generation exercise name normalizer.

After the LLM generates a plan, we normalize each exercise name against the
MuscleWiki exercise catalog (cached in Redis) using fuzzy string matching.
This ensures links to musclewiki.com don't 404 due to minor name differences.

Caching strategy:
  - Redis key:  musclewiki:exercise_names  (Redis SET, string members)
  - TTL:        30 days (well within the 30-day metadata cache allowed by ToS §3)
  - On cache miss: fetch live from MuscleWiki API, repopulate, then match.
"""
import difflib
import json
import logging

import httpx

from app.config import settings
from app.redis import redis_pool

logger = logging.getLogger(__name__)

_CACHE_KEY = "musclewiki:exercise_names"
_CACHE_TTL = 60 * 60 * 24 * 30  # 30 days in seconds
_API_BASE = "https://api.musclewiki.com"
_FUZZY_CUTOFF = 0.72


async def _fetch_all_exercise_names() -> list[str]:
    """Paginate MuscleWiki /exercises endpoint and return all exercise names."""
    names: list[str] = []
    offset = 0
    limit = 100
    async with httpx.AsyncClient(timeout=20) as client:
        while True:
            try:
                resp = await client.get(
                    f"{_API_BASE}/exercises",
                    headers={"X-API-Key": settings.MUSCLEWIKI_API_KEY},
                    params={"limit": limit, "offset": offset},
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.error(f"[exercise_normalizer] MuscleWiki fetch error at offset={offset}: {e}")
                break

            # API may return a list directly or a dict with a "results" key
            if isinstance(data, list):
                batch = data
            elif isinstance(data, dict):
                batch = data.get("results") or data.get("exercises") or data.get("data") or []
            else:
                break

            if not batch:
                break

            for ex in batch:
                name = ex.get("name") or ex.get("title") or ""
                if name:
                    names.append(name)

            if len(batch) < limit:
                break
            offset += limit

    logger.info(f"[exercise_normalizer] Fetched {len(names)} exercise names from MuscleWiki")
    return names


async def warm_exercise_cache() -> int:
    """Fetch all exercise names and store in Redis. Returns count stored."""
    names = await _fetch_all_exercise_names()
    if not names:
        logger.warning("[exercise_normalizer] warm_exercise_cache: no names fetched, skipping")
        return 0

    pipe = redis_pool.pipeline()
    pipe.delete(_CACHE_KEY)
    for name in names:
        pipe.sadd(_CACHE_KEY, name)
    pipe.expire(_CACHE_KEY, _CACHE_TTL)
    await pipe.execute()
    logger.info(f"[exercise_normalizer] Cached {len(names)} exercise names (TTL=30d)")
    return len(names)


async def _get_cached_names() -> list[str]:
    """Return cached exercise names, fetching from API if cache is empty."""
    members = await redis_pool.smembers(_CACHE_KEY)
    if members:
        return list(members)

    # Cache miss — re-warm
    logger.info("[exercise_normalizer] Cache miss, warming exercise cache...")
    await warm_exercise_cache()
    members = await redis_pool.smembers(_CACHE_KEY)
    return list(members)


async def normalize_exercise_name(name: str) -> str:
    """Fuzzy-match a single exercise name against the MuscleWiki catalog.

    Returns the best matching canonical name, or the original if no close
    match is found (cutoff=0.72).
    """
    if not name:
        return name

    candidates = await _get_cached_names()
    if not candidates:
        return name

    matches = difflib.get_close_matches(name, candidates, n=1, cutoff=_FUZZY_CUTOFF)
    if matches:
        normalized = matches[0]
        if normalized != name:
            logger.debug(f"[exercise_normalizer] '{name}' → '{normalized}'")
        return normalized
    return name


async def normalize_plan_exercises(plan_json: dict) -> dict:
    """Walk the plan JSON and normalize every exercise name in-place.

    Input structure:
        {"days": [{"exercises": [{"name": "...", ...}]}]}
    """
    for day in plan_json.get("days", []):
        for ex in day.get("exercises", []):
            original = ex.get("name", "")
            if original:
                ex["name"] = await normalize_exercise_name(original)
    return plan_json
