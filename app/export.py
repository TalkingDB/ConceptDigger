"""
Writes /dig-format records to JSON files, one per seed category, under an
`exports/` folder next to the graph cache (e.g. data/exports/).
"""
import json
import os
import re
from datetime import datetime
from typing import Dict, List, Set, Tuple

from .graph_store import GraphStore


def export_dir(store: GraphStore) -> str:
    return os.path.join(os.path.dirname(store.cache_file_path) or ".", "exports")


def write_exports(store: GraphStore, seeds: List[str], records: List[Dict[str, str]]) -> List[str]:
    """Write one timestamped file per seed, e.g. Food_ingredients_20261021T153000.json."""
    directory = export_dir(store)
    os.makedirs(directory, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    paths = []
    for seed in dict.fromkeys(seeds):
        name = re.sub(r"[^\w.-]", "_", seed.removeprefix("Category:"))
        path = os.path.join(directory, f"{name}_{stamp}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump([r for r in records if r["seed_category"] == seed], f, ensure_ascii=False, indent=2)
        paths.append(path)
    return paths


def collect_from_cache(
    store: GraphStore, seed: str, max_depth: int, return_categories: bool, return_pages: bool,
) -> List[Dict[str, str]]:
    """Same traversal as /dig, but over cached edges only — no SPARQL."""
    matched: Set[str] = set()

    def walk(node: str, depth: int) -> None:
        for child in store.children(node):
            if child.startswith("Category:"):
                if return_categories:
                    matched.add(child)
                if depth < max_depth:
                    walk(child, depth + 1)
            elif return_pages:
                matched.add(child)

    walk(seed, 0)
    return [
        {
            "entity_url": f"DBPedia>{uri}",
            "entity": uri,
            "surface_text": text,
            "seed_category": seed,
            "how_this_record": source,
        }
        for uri in matched
        for text, source in store.synonyms(uri)
    ]
