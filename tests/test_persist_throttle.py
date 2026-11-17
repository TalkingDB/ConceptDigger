"""GraphStore.persist is throttled by persist_interval_seconds; force always writes."""
import json

from app.graph_store import GraphStore


def _cached_nodes(path):
    with open(path, encoding="utf-8") as f:
        return set(json.load(f)["nodes"])


async def test_persist_within_interval_is_skipped_until_forced(tmp_path):
    path = tmp_path / "graph_cache.json"
    store = GraphStore(str(path), persist_interval_seconds=3600)
    async with store.write_lock():
        store.ensure_node("Category:A", "rdfs:label")
        await store.persist()  # first write: nothing written before
        store.ensure_node("Category:B", "rdfs:label")
        await store.persist()  # inside the interval: skipped
    assert _cached_nodes(path) == {"Category:A"}

    async with store.write_lock():
        await store.persist(force=True)
    assert _cached_nodes(path) == {"Category:A", "Category:B"}


async def test_zero_interval_writes_every_time(tmp_path):
    path = tmp_path / "graph_cache.json"
    store = GraphStore(str(path))  # default 0: the old behaviour
    async with store.write_lock():
        store.ensure_node("Category:A", "rdfs:label")
        await store.persist()
        store.ensure_node("Category:B", "rdfs:label")
        await store.persist()
    assert _cached_nodes(path) == {"Category:A", "Category:B"}
