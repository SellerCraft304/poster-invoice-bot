"""
Persistent mapping cache: original invoice name → Poster ingredient.
Stored as JSON. On Railway, set CACHE_FILE env var to a volume path.
"""
import json
import logging
import os

logger = logging.getLogger(__name__)
CACHE_FILE = os.environ.get("CACHE_FILE", "/tmp/mapping_cache.json")


def _load() -> dict:
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(data: dict):
    try:
        dir_path = os.path.dirname(CACHE_FILE)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"Cache save failed: {e}")


def get_cached(original_name: str) -> "dict | None":
    """Returns {"ingredient_id": ..., "ingredient_name": ...} or None."""
    key = original_name.lower().strip()
    return _load().get(key)


def save_match(original_name: str, ingredient_id, ingredient_name: str):
    cache = _load()
    key = original_name.lower().strip()
    cache[key] = {"ingredient_id": ingredient_id, "ingredient_name": ingredient_name}
    _save(cache)
    logger.info(f"Cached: '{key}' → {ingredient_name} (id={ingredient_id})")


def delete_match(original_name: str):
    cache = _load()
    key = original_name.lower().strip()
    cache.pop(key, None)
    _save(cache)


def all_cached() -> dict:
    return _load()
