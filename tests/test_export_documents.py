"""
Offline checks of the /dig and /export document shape: one document per
(entity, seed category) with its synonyms grouped under surface_text.
"""
import json

from app.export import collect_from_cache, write_exports
from app.graph_store import GraphStore

SEED = "Category:Food_ingredients"


def _store(tmp_path) -> GraphStore:
    store = GraphStore(str(tmp_path / "graph_cache.json"))
    for uri in (SEED, "Category:Spices", "Ben_oil", "Turmeric"):
        store.ensure_node(uri, "rdfs:label")
    store.ensure_edge(SEED, "Category:Spices")
    store.ensure_edge(SEED, "Ben_oil")
    store.ensure_edge("Category:Spices", "Turmeric")
    store.add_synonyms("Ben_oil", [("behen oil", "redirect"), ("oil of ben", "redirect")])
    return store


def test_documents_group_surface_text_per_entity(tmp_path):
    docs = collect_from_cache(_store(tmp_path), SEED, 0, return_categories=True, return_pages=True)
    by_entity = {d["entity"]: d for d in docs}

    assert set(by_entity) == {"Category:Spices", "Ben_oil"}
    assert by_entity["Ben_oil"] == {
        "entity_url": "DBPedia>Ben_oil",
        "entity": "Ben_oil",
        "surface_text": [
            {"text": "ben oil", "how_this_record": "rdfs:label"},
            {"text": "behen oil", "how_this_record": "redirect"},
            {"text": "oil of ben", "how_this_record": "redirect"},
        ],
        "seed_category": SEED,
    }


def test_entities_without_synonyms_are_omitted(tmp_path):
    store = _store(tmp_path)
    store.graph.nodes["Ben_oil"]["synonyms"] = []
    docs = collect_from_cache(store, SEED, 0, return_categories=False, return_pages=True)
    assert docs == []


def test_export_file_holds_the_documents(tmp_path):
    store = _store(tmp_path)
    docs = collect_from_cache(store, SEED, 1, return_categories=True, return_pages=True)
    [path] = write_exports(store, [SEED], docs)
    with open(path, encoding="utf-8") as f:
        written = json.load(f)
    assert sorted(d["entity"] for d in written) == ["Ben_oil", "Category:Spices", "Turmeric"]
    assert written == docs
