"""
Writes /dig-format records to JSON files, one per seed category, under an
`exports/` folder next to the graph cache (e.g. data/exports/).
"""
import json
import logging
import os
import re
from datetime import datetime
from typing import Dict, List, Set
from uuid import uuid4

from .config import settings
from .graph_store import GraphStore

logger = logging.getLogger(__name__)


def export_dir(store: GraphStore) -> str:
    return os.path.join(os.path.dirname(store.cache_file_path) or ".", "exports")


def _prune_old_exports(directory: str, keep: int, protect: List[str]) -> None:
    """Keep the newest `keep` files; never delete files written in this call."""
    if keep <= 0:
        return
    try:
        files = [
            os.path.join(directory, name)
            for name in os.listdir(directory)
            if name.endswith(".json")
        ]
    except OSError:
        logger.exception("export cleanup could not list %s", directory)
        return
    protected = {os.path.abspath(path) for path in protect}
    files.sort(key=os.path.getmtime, reverse=True)
    keep_set = {os.path.abspath(path) for path in files[:keep]} | protected
    for path in files:
        if os.path.abspath(path) in keep_set:
            continue
        try:
            os.remove(path)
        except OSError:
            logger.warning("could not remove old export %s", path, exc_info=True)


def write_exports(store: GraphStore, seeds: List[str], records: List[Dict[str, str]]) -> List[str]:
    """Write one timestamped file per seed, e.g. Food_ingredients_20261021T153000123456_ab12cd34.json."""
    directory = export_dir(store)
    os.makedirs(directory, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    paths = []
    for seed in dict.fromkeys(seeds):
        name = re.sub(r"[^\w.-]", "_", seed.removeprefix("Category:"))
        path = os.path.join(directory, f"{name}_{stamp}_{uuid4().hex[:8]}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump([r for r in records if r["seed_category"] == seed], f, ensure_ascii=False, indent=2)
        paths.append(path)
    _prune_old_exports(directory, settings.export_max_files, paths)
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
