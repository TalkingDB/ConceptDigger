"""
POST /api/v1/dig — digs one or more DBpedia categories to a shared max_depth,
collecting subcategories and/or member articles per category, using the
corrected seed_category attribution (this used to be the separate
`/dig/fixed` endpoint; the legacy buggy `/dig` has been retired — there is
now only one dig endpoint and it's always correct).

Request body:
{
    "categories": ["Category:Birds", "Category:Fish"],
    "max_depth": 2,
    "return_categories": true,
    "return_pages": true,
    "export": false
}

max_depth / return_categories / return_pages apply to every category in the
batch (this is a change from the old per-item-tuple protocol, where each
category could carry its own depth/flags). max_depth may not exceed
MAX_DEPTH_HARD_CAP; values above the cap are rejected with HTTP 400.

Response body: a JSON array of
{entity_url, entity, surface_text, seed_category, how_this_record} objects.
When export is true, the same records are also written to
data/exports/<Seed>_<timestamp>_<id>.json, one file per seed category.
A write failure is logged and does not fail /dig.

POST /api/v1/export reads only the cached graph (no SPARQL) and writes that
file on demand for a single category; it returns the paths written. Its body
is just the category and an optional max_depth (categories and pages are
always included):
{
  "category": "http://dbpedia.org/resource/Category:Food_ingredients",
  "max_depth": 2
}
"""
import asyncio
import logging
from typing import List

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..category import require_category, require_max_depth
from ..config import settings
from ..dig import run_dig
from ..export import collect_from_cache, write_exports
from ..hydration import SparqlBudget

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["dig"])


class DigRequest(BaseModel):
    categories: List[str] = Field(
        ...,
        min_length=1,
        description="DBpedia/Wikipedia category URLs (or Category: local names) to dig.",
    )
    max_depth: int = Field(
        1,
        ge=0,
        description=(
            f"How many levels deep to traverse. Must not exceed "
            f"{settings.max_depth_hard_cap}."
        ),
    )
    return_categories: bool = Field(True, description="Include matched subcategories in the output.")
    return_pages: bool = Field(True, description="Include matched member articles in the output.")
    export: bool = Field(
        False,
        description="If true, also write JSON files under data/exports/. Default false.",
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "categories": ["http://dbpedia.org/resource/Category:Food_ingredients"],
                "max_depth": 2,
                "return_categories": True,
                "return_pages": True,
                "export": False,
            }
        }
    }


class ExportRequest(BaseModel):
    category: str = Field(
        ...,
        description="DBpedia/Wikipedia category URL (or Category: local name) to export from the cache.",
    )
    max_depth: int = Field(
        1,
        ge=0,
        description=(
            f"How many levels deep to traverse. Must not exceed "
            f"{settings.max_depth_hard_cap}."
        ),
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "category": "http://dbpedia.org/resource/Category:Food_ingredients",
                "max_depth": 2,
            }
        }
    }


@router.post("/dig")
async def dig(body: DigRequest, request: Request):
    store = request.app.state.store
    hydration = request.app.state.hydration
    budget = SparqlBudget()

    max_depth = require_max_depth(body.max_depth)
    items = [
        (
            require_category(category),
            max_depth,
            int(body.return_categories),
            int(body.return_pages),
        )
        for category in body.categories
    ]

    records = await run_dig(hydration, store, items, budget, fixed=True)
    if body.export:
        try:
            await asyncio.to_thread(write_exports, store, [item[0] for item in items], records)
        except Exception:
            logger.exception("Failed to write /dig export files; returning records anyway")
    return records


@router.post("/export")
async def export(body: ExportRequest, request: Request):
    store = request.app.state.store
    max_depth = require_max_depth(body.max_depth)
    seed = require_category(body.category)
    if not store.has_node(seed):
        raise HTTPException(status_code=404, detail=f"Category not in cache: {seed}")
    records = collect_from_cache(store, seed, max_depth, return_categories=True, return_pages=True)
    return {"files": await asyncio.to_thread(write_exports, store, [seed], records)}
