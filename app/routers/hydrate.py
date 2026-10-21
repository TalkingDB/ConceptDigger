"""
POST /api/v1/hydrate-category  — replaces legacy `insert-nodes`. Pre-warms
    the cache for one category (and optionally several levels of its
    subcategories) directly, without needing a /dig call to trigger it.

POST /api/v1/hydrate-synonyms  — replaces legacy `assign-synonym-from-*`.
    Pre-warms label/alt-name/redirect/disambiguation synonyms for one
    entity (category or page).

Both report how many live SPARQL calls they made (`sparql_calls_used`).
"""
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ..category import require_category, require_max_depth, require_resource
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

    return {
        "category": seed,
        "max_depth": max_depth,
        "new_nodes": total_new_nodes,
        "new_edges": total_new_edges,
        "sparql_calls_used": budget.used,
    }


@router.post("/hydrate-synonyms")
async def hydrate_synonyms(body: HydrateSynonymsRequest, request: Request):
    hydration = request.app.state.hydration
    entity = require_resource(body.entity)
    budget = SparqlBudget()
    added = await hydration.hydrate_synonyms(entity, budget)
    return {
        "entity": entity,
        "synonyms_added": added,
        "sparql_calls_used": budget.used,
    }
