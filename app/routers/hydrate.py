"""
POST /api/v1/hydrate-category  — replaces legacy `insert-nodes`. Pre-warms
    the cache for one category (and optionally several levels of its
    subcategories) directly, without needing a /dig call to trigger it.

POST /api/v1/hydrate-synonyms  — replaces legacy `assign-synonym-from-*`.
    Pre-warms label/alt-name/redirect/disambiguation synonyms for one
    entity (category or page).

Both are budgeted the same way /dig is, so a broad pre-warm can't fan out
into an unbounded live-SPARQL crawl either.
"""
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ..category import require_category, require_max_depth
from ..config import settings
from ..hydration import SparqlBudget

router = APIRouter(prefix="/api/v1", tags=["hydrate"])


class HydrateCategoryRequest(BaseModel):
    category: str = Field(..., description="DBpedia/Wikipedia category URL or Category: local name.")
    max_depth: int = Field(1, ge=0, description="How many levels of subcategories to pre-warm.")


class HydrateSynonymsRequest(BaseModel):
    entity: str


@router.post("/hydrate-category")
async def hydrate_category(body: HydrateCategoryRequest, request: Request):
    store = request.app.state.store
    hydration = request.app.state.hydration
    seed = require_category(body.category)
    max_depth = require_max_depth(body.max_depth)
    budget = SparqlBudget(settings.max_sparql_calls_per_request)

    frontier = [seed]
    total_new_nodes = 0
    total_new_edges = 0

    for _level in range(max(max_depth, 1)):
        next_frontier = []
        for node in frontier:
            new_nodes, new_edges = await hydration.hydrate_category_structure(node, budget)
            total_new_nodes += new_nodes
            total_new_edges += new_edges
            if not budget.has_room():
                break
            next_frontier.extend(c for c in store.children(node) if c.startswith("Category:"))
        frontier = next_frontier
        if not budget.has_room() or not frontier:
            break

    return {
        "category": seed,
        "max_depth": max_depth,
        "new_nodes": total_new_nodes,
        "new_edges": total_new_edges,
        "sparql_calls_used": budget.used,
        "budget_exhausted": budget.exhausted,
    }


@router.post("/hydrate-synonyms")
async def hydrate_synonyms(body: HydrateSynonymsRequest, request: Request):
    hydration = request.app.state.hydration
    budget = SparqlBudget(settings.max_sparql_calls_per_request)
    added = await hydration.hydrate_synonyms(body.entity, budget)
    return {
        "entity": body.entity,
        "synonyms_added": added,
        "sparql_calls_used": budget.used,
        "budget_exhausted": budget.exhausted,
    }
