"""
POST /api/v1/hydrate-category  — replaces legacy `insert-nodes`. Pre-warms
    the cache for one category (and optionally several levels of its
    subcategories) directly, without needing a /dig call to trigger it.

POST /api/v1/hydrate-synonyms  — replaces legacy `assign-synonym-from-*`.
    Pre-warms label/alt-name/redirect/disambiguation synonyms for one
    entity (category or page).

Both report how many live SPARQL calls they made (`sparql_calls_used`).

POST /api/v1/hydrate-abstracts — backfills the Wikipedia abstract of every
    page already cached under a root category (no SPARQL calls).
"""
from dataclasses import asdict
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..category import require_category, require_max_depth, require_resource
from ..config import settings
from ..hydration import SparqlBudget

router = APIRouter(prefix="/api/v1", tags=["hydrate"])


class HydrateCategoryRequest(BaseModel):
    category: str = Field(..., description="DBpedia/Wikipedia category URL or Category: local name.")
    max_depth: int = Field(1, ge=0, description="How many levels of subcategories to pre-warm.")


class HydrateSynonymsRequest(BaseModel):
    entity: str = Field(
        ...,
        description="DBpedia/Wikipedia URL or local name of one category or article.",
    )


@router.post("/hydrate-category")
async def hydrate_category(body: HydrateCategoryRequest, request: Request):
    store = request.app.state.store
    hydration = request.app.state.hydration
    seed = require_category(body.category)
    max_depth = require_max_depth(body.max_depth)
    budget = SparqlBudget()

    frontier = [seed]
    total_new_nodes = 0
    total_new_edges = 0

    for _level in range(max(max_depth, 1)):
        next_frontier = []
        for node in frontier:
            new_nodes, new_edges = await hydration.hydrate_category_structure(node, budget)
            total_new_nodes += new_nodes
            total_new_edges += new_edges
            next_frontier.extend(c for c in store.children(node) if c.startswith("Category:"))
        frontier = next_frontier
        if not frontier:
            break
    async with store.write_lock():  # the throttled writes may have skipped the last hydrations
        await store.persist(force=True)

    return {
        "category": seed,
        "max_depth": max_depth,
        "new_nodes": total_new_nodes,
        "new_edges": total_new_edges,
        "sparql_calls_used": budget.used,
    }


class HydrateAbstractsRequest(BaseModel):
    root: str = Field(..., description="Category whose cached pages get abstracts, e.g. 'Category:Food_ingredients'.")
    depth: int = Field(6, ge=0, description="How many levels of cached children to walk, as in /graph/data.")
    refresh: bool = Field(False, description="Re-fetch abstracts that were already fetched.")
    limit: Optional[int] = Field(
        None, ge=1,
        description="Most pages to fetch in this call (default ABSTRACTS_PER_REQUEST). Repeat the call "
                    "until `remaining` is 0.",
    )


@router.post("/hydrate-abstracts")
async def hydrate_abstracts(body: HydrateAbstractsRequest, request: Request):
    """Backfill Wikipedia abstracts for pages already cached under `root`, up to
    `limit` per call; repeat until `remaining` is 0. One call per root at a time
    (409 otherwise). Never hydrates structure or synonyms; run /dig or
    /hydrate-category first."""
    store = request.app.state.store
    hydration = request.app.state.hydration
    root = require_resource(body.root)
    depth = min(body.depth, settings.max_depth_hard_cap + 1)
    if not store.has_node(root):
        raise HTTPException(
            status_code=404,
            detail=f"{root} is not in the cached graph. Run /dig or /hydrate-category first.",
        )
    if root in hydration.abstract_roots_running:
        raise HTTPException(status_code=409, detail=f"Abstracts for {root} are already being fetched; "
                                                    "call again when that call has finished.")
    limit = body.limit or settings.abstracts_per_request
    hydration.abstract_roots_running.add(root)
    try:
        report = await hydration.hydrate_abstracts(store.descendants(root, depth), refresh=body.refresh,
                                                   limit=limit)
    finally:
        hydration.abstract_roots_running.discard(root)
    return {"root": root, "depth": depth, "refresh": body.refresh, "limit": limit, **asdict(report)}


@router.post("/hydrate-synonyms")
async def hydrate_synonyms(body: HydrateSynonymsRequest, request: Request):
    store = request.app.state.store
    hydration = request.app.state.hydration
    entity = require_resource(body.entity)
    budget = SparqlBudget()
    added = await hydration.hydrate_synonyms(entity, budget)
    async with store.write_lock():  # the throttled writes may have skipped it
        await store.persist(force=True)
    return {
        "entity": entity,
        "synonyms_added": added,
        "sparql_calls_used": budget.used,
    }
