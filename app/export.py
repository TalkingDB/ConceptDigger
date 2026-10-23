"""
Writes /dig-format documents to JSON files, one per seed category, under an
`exports/` folder next to the graph cache (e.g. data/exports/).
"""
import json
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Set

from .dig import build_documents
from .graph_store import GraphStore


_STAMP = re.compile(r"_(\d{8}T\d{6})\.json$")


def export_dir(store: GraphStore) -> str:
    return os.path.join(os.path.dirname(store.cache_file_path) or ".", "exports")


def list_export_files(store: GraphStore) -> List[str]:
    """Export file names, newest first.

    The timestamp embedded in the name (`Seed_YYYYmmddTHHMMSS.json`) is the
    export time. Files without that stamp fall back to modification time.
    """
    directory = export_dir(store)
    if not os.path.isdir(directory):
        return []

    def sort_key(name: str) -> str:
        match = _STAMP.search(name)
        if match:
            return match.group(1)
        mtime = os.path.getmtime(os.path.join(directory, name))
        return datetime.fromtimestamp(mtime).strftime("%Y%m%dT%H%M%S")

    names = [
        name for name in os.listdir(directory)
        if name.endswith(".json") and os.path.isfile(os.path.join(directory, name))
    ]
    names.sort(key=sort_key, reverse=True)
    return names


def write_exports(store: GraphStore, seeds: List[str], records: List[Dict[str, Any]]) -> List[str]:
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
) -> List[Dict[str, Any]]:
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
    return build_documents(store, ((uri, seed) for uri in matched))
