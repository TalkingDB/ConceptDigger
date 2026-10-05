"""
Core traversal logic for /api/v1/dig

The Endpoint walk the same descendant tree from each seed category, up to
max_depth, collecting categories and/or pages per the return_categories /
return_pages flags — this mirrors the legacy enumerateChild()/prepareOutput()
pair exactly, including recursing into every category regardless of whether
categories are being *returned*, since a category might still contain pages
deeper down that should be returned.
"""
import logging
from typing import Dict, List, Set, Tuple
from fastapi import HTTPException

from .config import settings
from .graph_store import GraphStore
from .hydration import HydrationService, SparqlBudget

logger = logging.getLogger(__name__)

DigItem = Tuple[str, int, int, int]


async def _enumerate_descendants(
    hydration: HydrationService,
    store: GraphStore,
    node: str,
    cur_depth: int,
    max_depth: int,
    return_categories: bool,
    return_pages: bool,
    budget: SparqlBudget,
    seed: str,
    collected: List[Tuple[str, str]],
) -> None:
    if not store.is_structure_hydrated(node):
        await hydration.hydrate_category_structure(node, budget)

    for child in store.children(node):
        is_category = child.startswith("Category:")
        if is_category:
            if return_categories:
                collected.append((child, seed))
                if not store.is_synonyms_hydrated(child):
                    await hydration.hydrate_synonyms(child, budget)
            if cur_depth < max_depth:
                await _enumerate_descendants(
                    hydration, store, child, cur_depth + 1, max_depth,
                    return_categories, return_pages, budget, seed, collected,
                )
        else:
            if return_pages:
                collected.append((child, seed))
                if not store.is_synonyms_hydrated(child):
                    await hydration.hydrate_synonyms(child, budget)


def _prepare_output_legacy(store: GraphStore, matched: Set[str]) -> List[Dict[str, str]]:
    output = []
    for uri in matched:
        for text, source in store.synonyms(uri):
            output.append({
                "entity_url": f"DBPedia>{uri}",
                "surface_text": text,
                # Bug preserved on purpose: this is the matched node's own
                # name, not the seed category that was actually dug.
                "seed_category": uri,
                "how_this_record": source,
            })
    return output


def _prepare_output_fixed(store: GraphStore, matched: Set[Tuple[str, str]]) -> List[Dict[str, str]]:
    output = []
    for uri, seed in matched:
        for text, source in store.synonyms(uri):
            output.append({
                "entity_url": f"DBPedia>{uri}",
                "surface_text": text,
                "seed_category": seed,
                "how_this_record": source,
            })
    return output


async def run_dig(
    hydration: HydrationService,
    store: GraphStore,
    items: List[DigItem],
    budget: SparqlBudget,
    fixed: bool,
) -> List[Dict[str, str]]:
    matched_legacy: Set[str] = set()
    matched_fixed: Set[Tuple[str, str]] = set()

    for seed_category, max_depth_raw, return_categories_raw, return_pages_raw in items:
        max_depth = min(int(max_depth_raw), settings.max_depth_hard_cap)
        return_categories = bool(int(return_categories_raw))
        return_pages = bool(int(return_pages_raw))

        if not await hydration.category_exists(seed_category, budget):
            logger.warning(
                "skipping seed, doesn't look like a real category: %s",
                seed_category,
            )
            raise HTTPException(
                status_code=404,
                detail=f"Category not found: {seed_category}",
            )

        if not store.is_structure_hydrated(seed_category):
            await hydration.hydrate_category_structure(seed_category, budget)
        if not store.has_node(seed_category):
            # Category doesn't exist even after a live hydration attempt
            # (unknown seed, or budget ran out before we could create it) —
            # skip it rather than crash the whole batch.
            continue

        collected: List[Tuple[str, str]] = []
        await _enumerate_descendants(
            hydration, store, seed_category, 0, max_depth,
            return_categories, return_pages, budget, seed_category, collected,
        )

        if fixed:
            matched_fixed.update(collected)
        else:
            matched_legacy.update(child for child, _seed in collected)

    all_uris = matched_legacy if not fixed else {child for child, _seed in matched_fixed}
    for uri in all_uris:
        if not store.is_synonyms_hydrated(uri):
            await hydration.hydrate_synonyms(uri, budget)

    if budget.exhausted:
        logger.warning(
            "dig batch finished with SPARQL budget exhausted: seeds=%d matched=%d used=%d limit=%d "
            "— some results may be under-enriched or incomplete",
            len(items), len(all_uris), budget.used, budget.limit,
        )
    else:
        logger.info(
            "dig batch finished within budget: seeds=%d matched=%d used=%d limit=%d",
            len(items), len(all_uris), budget.used, budget.limit,
        )

    if fixed:
        return _prepare_output_fixed(store, matched_fixed)
    return _prepare_output_legacy(store, matched_legacy)