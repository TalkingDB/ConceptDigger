"""
store.py — JSON file persistence layer for DBpedia Digger
==========================================================

Structure of digger_store.json on disk:
{
  "entities": {
    "Waterproofing": {
      "entity_name": "Waterproofing",
      "synonyms": ["waterproofing", "waterproof protection", ...],
      "seed_categories": ["Waterproofing", "Moisture_protection"],
      "last_updated": "2025-02-20T14:30:00"
    },
    ...
  }
}

Thread/async safety: all reads and writes go through an asyncio.Lock so
concurrent requests never produce a half-written file.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Path to the JSON store file — change via STORE_PATH config in main.py if needed
DEFAULT_STORE_PATH = Path("digger_store.json")

_EMPTY_STORE: dict = {"entities": {}}


class EntityStore:
    """
    Async-safe, file-backed key-value store.
    One instance is created at startup and shared across all requests via app.state.
    """

    def __init__(self, path: Path = DEFAULT_STORE_PATH) -> None:
        self.path  = path
        self._lock = asyncio.Lock()
        self._data: dict = _EMPTY_STORE.copy()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Load existing store from disk (called once at startup, synchronously)."""
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
                log.info("Store loaded from %s  (%d entities)", self.path, len(self._data.get("entities", {})))
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("Could not read store file %s: %s — starting fresh.", self.path, exc)
                self._data = _EMPTY_STORE.copy()
        else:
            log.info("No existing store at %s — will create on first save.", self.path)
            self._data = _EMPTY_STORE.copy()

    def _save_sync(self) -> None:
        """Write current in-memory state to disk (called while lock is held)."""
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)
        tmp.replace(self.path)   # atomic rename — avoids half-written files

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    async def upsert_entities(
        self,
        entities: list[dict],   # list of {"entity_name": str, "synonyms": list[str]}
        seed_category: str,
    ) -> int:
        """
        Merge a list of entities into the store.
        - New entities are added.
        - Existing entities have their synonym lists merged (deduplicated).
        - seed_categories list is updated to track which digs produced each entity.
        Returns the number of entities that were new (not previously in store).
        """
        new_count = 0
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")

        async with self._lock:
            store_entities: dict = self._data.setdefault("entities", {})

            for item in entities:
                name = item["entity_name"]
                incoming_synonyms: list[str] = item.get("synonyms", [])

                if name not in store_entities:
                    # Brand new entity
                    store_entities[name] = {
                        "entity_name"     : name,
                        "synonyms"        : list(incoming_synonyms),
                        "seed_categories" : [seed_category],
                        "last_updated"    : now,
                    }
                    new_count += 1
                else:
                    existing = store_entities[name]

                    # Merge synonyms (case-insensitive dedup)
                    existing_lower = {s.lower() for s in existing["synonyms"]}
                    for syn in incoming_synonyms:
                        if syn.lower() not in existing_lower:
                            existing["synonyms"].append(syn)
                            existing_lower.add(syn.lower())

                    # Track which seed categories contributed to this entity
                    if seed_category not in existing.get("seed_categories", []):
                        existing.setdefault("seed_categories", []).append(seed_category)

                    existing["last_updated"] = now

            self._save_sync()

        log.info("Upserted %d entities (%d new) from seed '%s'", len(entities), new_count, seed_category)
        return new_count

    async def delete_entity(self, entity_name: str) -> bool:
        """Remove a single entity. Returns True if it existed."""
        async with self._lock:
            existed = entity_name in self._data.get("entities", {})
            if existed:
                del self._data["entities"][entity_name]
                self._save_sync()
        return existed

    async def delete_by_category(self, seed_category: str) -> int:
        """
        Remove all entities whose seed_categories list contains seed_category.
        Returns count of removed entities.
        """
        async with self._lock:
            store_entities: dict = self._data.get("entities", {})
            to_remove = [
                name for name, val in store_entities.items()
                if seed_category in val.get("seed_categories", [])
            ]
            for name in to_remove:
                del store_entities[name]
            if to_remove:
                self._save_sync()
        log.info("Deleted %d entities seeded by '%s'", len(to_remove), seed_category)
        return len(to_remove)

    # ------------------------------------------------------------------
    # Read operations  (no lock needed — reads are safe in CPython,
    # and writes always go through the lock + atomic rename)
    # ------------------------------------------------------------------

    def get_all(self, category_filter: Optional[str] = None) -> list[dict]:
        """
        Return all stored entities, optionally filtered by seed_category.
        Each item has: entity_name, synonyms, seed_categories, last_updated.
        """
        entities = self._data.get("entities", {}).values()
        if category_filter:
            entities = [
                e for e in entities
                if category_filter in e.get("seed_categories", [])
            ]
        return list(entities)

    def get_one(self, entity_name: str) -> Optional[dict]:
        """Return a single entity by name, or None if not found."""
        return self._data.get("entities", {}).get(entity_name)

    def list_categories(self) -> list[str]:
        """Return a sorted list of every seed_category ever stored."""
        cats: set[str] = set()
        for entity in self._data.get("entities", {}).values():
            cats.update(entity.get("seed_categories", []))
        return sorted(cats)

    def stats(self) -> dict:
        entities   = self._data.get("entities", {})
        all_syns   = [s for e in entities.values() for s in e.get("synonyms", [])]
        return {
            "total_entities" : len(entities),
            "total_synonyms" : len(all_syns),
            "seed_categories": self.list_categories(),
        }