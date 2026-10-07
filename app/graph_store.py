"""
GraphStore wraps a single networkx.DiGraph that is the whole in-process
"database": nodes are DBPedia resource local names (e.g. "Category:Birds",
"Chicken"), edges point parent -> child (broader category -> narrower
category / member article), matching the direction the legacy code built.

Node attributes:
    synonyms:            list[[surface_text, source_tag]]
    structure_hydrated:   bool — have we fetched this node's children from SPARQL?
    synonyms_hydrated:    bool — have we fetched this node's labels/alt-names/
                           redirects/disambiguations from SPARQL?

Design notes:
- The graph boots empty and is populated lazily. On startup we try to load a
  previously persisted JSON cache; if none exists, we start from nothing —
  the very first /dig naming an unseen category is what triggers hydration.
- All graph *writes* go through `write_lock()` so two coroutines can't
  interleave partial updates.
- `run_deduped` is the de-dup mechanism for hydration: if two concurrent
  requests both want to hydrate the same category/entity at the same time,
  the second one just awaits the first one's in-flight result instead of
  re-firing the same SPARQL calls.
"""
import asyncio
import json
import os
from typing import Callable, Coroutine, Dict, List, Tuple

import networkx as nx

from .utils import convert_entity_uri_to_label


class GraphStore:
    def __init__(self, cache_file_path: str):
        self.cache_file_path = cache_file_path
        self.graph = nx.DiGraph()
        self._write_lock = asyncio.Lock()
        self._inflight: Dict[str, "asyncio.Future"] = {}
        self._inflight_lock = asyncio.Lock()
        self._load()

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------
    def _load(self) -> None:
        if not os.path.exists(self.cache_file_path):
            return
        with open(self.cache_file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for uri, attrs in data.get("nodes", {}).items():
            self.graph.add_node(uri, **attrs)
        for parent, child in data.get("edges", []):
            self.graph.add_edge(parent, child)

    async def persist(self) -> None:
        """Caller must already hold write_lock(). Writes atomically via a
        temp-file + rename so a crash mid-write can't corrupt the cache."""
        nodes = {uri: dict(attrs) for uri, attrs in self.graph.nodes(data=True)}
        edges = [[u, v] for u, v in self.graph.edges()]
        directory = os.path.dirname(self.cache_file_path) or "."
        os.makedirs(directory, exist_ok=True)
        tmp_path = self.cache_file_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({"nodes": nodes, "edges": edges}, f)
        os.replace(tmp_path, self.cache_file_path)

    # ------------------------------------------------------------------
    # write coordination
    # ------------------------------------------------------------------
    def write_lock(self) -> asyncio.Lock:
        return self._write_lock

    async def run_deduped(self, key: str, coro_fn: Callable[..., Coroutine], *args, **kwargs):
        """Run coro_fn(*args, **kwargs) at most once per `key` at a time.
        Concurrent callers with the same key await the same result."""
        async with self._inflight_lock:
            fut = self._inflight.get(key)
            owner = fut is None
            if owner:
                fut = asyncio.get_event_loop().create_future()
                self._inflight[key] = fut
        if not owner:
            return await fut
        try:
            result = await coro_fn(*args, **kwargs)
            fut.set_result(result)
            return result
        except Exception as exc:  # noqa: BLE001 - propagate to all waiters
            fut.set_exception(exc)
            raise
        finally:
            async with self._inflight_lock:
                self._inflight.pop(key, None)

    # ------------------------------------------------------------------
    # node/edge helpers (call while holding write_lock() for mutations)
    # ------------------------------------------------------------------
    def has_node(self, uri: str) -> bool:
        return self.graph.has_node(uri)

    def is_structure_hydrated(self, uri: str) -> bool:
        return self.graph.has_node(uri) and self.graph.nodes[uri].get("structure_hydrated", False)

    def is_synonyms_hydrated(self, uri: str) -> bool:
        return self.graph.has_node(uri) and self.graph.nodes[uri].get("synonyms_hydrated", False)

    def synonyms(self, uri: str) -> List[List[str]]:
        if not self.graph.has_node(uri):
            return []
        return self.graph.nodes[uri].get("synonyms", [])

    def children(self, uri: str) -> List[str]:
        if not self.graph.has_node(uri):
            return []
        return list(self.graph.successors(uri))

    def ensure_node(self, uri: str, source: str) -> bool:
        """Create the node (seeded with its URI-derived label as a baseline
        synonym) if it doesn't exist yet. Returns True if newly created."""
        if self.graph.has_node(uri):
            return False
        label = convert_entity_uri_to_label(uri)
        self.graph.add_node(
            uri,
            synonyms=[[label, source]],
            structure_hydrated=False,
            synonyms_hydrated=False,
        )
        return True

    def ensure_edge(self, parent: str, child: str) -> bool:
        if self.graph.has_edge(parent, child):
            return False
        self.graph.add_edge(parent, child)
        return True

    def mark_structure_hydrated(self, uri: str) -> None:
        self.graph.nodes[uri]["structure_hydrated"] = True

    def mark_synonyms_hydrated(self, uri: str) -> None:
        self.graph.nodes[uri]["synonyms_hydrated"] = True

    def add_synonyms(self, uri: str, synonyms: List[Tuple[str, str]]) -> int:
        node = self.graph.nodes[uri]
        existing = node.setdefault("synonyms", [])
        existing_texts = {s[0] for s in existing}
        added = 0
        for text, source in synonyms:
            if not text or text in existing_texts:
                continue
            existing.append([text, source])
            existing_texts.add(text)
            added += 1
        return added
