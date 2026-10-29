"""
Experimental: POST /api/v1/wikidata/dig — the Wikidata counterpart of /dig,
for comparing synonym coverage. See app/wikidata.py for how the class tree is
walked and where each synonym comes from. Results are cached in
data/wikidata_cache.json, separate from the DBpedia graph_cache.json.

Request body:
{
    "seeds": ["Q25403900"],
    "max_depth": 1,
    "return_categories": true,
    "return_pages": true,
    "only_with_wikipedia_article": false,
    "include_disambiguation": false
}

GET /api/v1/wikidata/search?q=food ingredient finds the QID to use as a seed.
"""
import asyncio
import contextlib
import logging
from typing import List

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..category import require_max_depth
from ..config import settings
from ..hydration import SparqlBudget
from ..wikidata import WikidataDigResponse, parse_qid, run_wikidata_dig

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/wikidata", tags=["wikidata (experimental)"])

DISCONNECT_POLL_SECONDS = 1.0


class WikidataDigRequest(BaseModel):
    seeds: List[str] = Field(
        ...,
        min_length=1,
        description="Wikidata classes to dig: Q25403900, wd:Q25403900 or a wikidata.org URL.",
    )
    max_depth: int = Field(
        1,
        ge=0,
        description=f"Levels to traverse (0 = direct children). Must not exceed {settings.max_depth_hard_cap}.",
    )
    return_categories: bool = Field(True, description="Include subclasses (P279) in the output.")
    return_pages: bool = Field(True, description="Include instances (P31) in the output.")
    only_with_wikipedia_article: bool = Field(
        False, description="Drop entities with no English Wikipedia article (closer to what DBpedia returns).",
    )
    include_disambiguation: bool = Field(
        False, description="Also add disambiguation pages linking to each article. One extra call per entity.",
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "seeds": ["Q25403900"],
                "max_depth": 1,
                "return_categories": True,
                "return_pages": True,
                "only_with_wikipedia_article": False,
                "include_disambiguation": False,
            }
        }
    }


@router.post("/dig", response_model=WikidataDigResponse)
async def wikidata_dig(body: WikidataDigRequest, request: Request):
    service = request.app.state.wikidata
    max_depth = require_max_depth(body.max_depth)
    budget = SparqlBudget()

    seeds = []
    for value in body.seeds:
        qid = parse_qid(value)
        if qid is None:
            raise HTTPException(
                status_code=400,
                detail=f"'{value}' is not a Wikidata QID. Find one with GET /api/v1/wikidata/search?q=...",
            )
        exists = await service.entity_exists(qid, budget)
        if exists is False:
            raise HTTPException(status_code=404, detail=f"Wikidata item not found: {qid}")
        if exists:
            seeds.append(qid)

    task = asyncio.create_task(run_wikidata_dig(
        service, seeds, max_depth,
        body.return_categories, body.return_pages,
        body.only_with_wikipedia_article, body.include_disambiguation,
        budget,
    ))
    # FastAPI keeps running a handler after the client disconnects, so the
    # dig would carry on fetching. Poll for the disconnect and cancel it.
    while not task.done():
        await asyncio.wait({task}, timeout=DISCONNECT_POLL_SECONDS)
        if not task.done() and await request.is_disconnected():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            logger.info("client disconnected, wikidata dig cancelled: seeds=%s", seeds)
            return Response(status_code=499)
    result = task.result()
    if result.status == "failed":
        return JSONResponse(status_code=502, content=result.model_dump(mode="json"))
    return result


@router.get("/search")
async def wikidata_search(request: Request, q: str = Query(..., min_length=1), limit: int = Query(10, ge=1, le=50)):
    """Look up candidate QIDs by name, e.g. q=food ingredient -> Q25403900."""
    return await request.app.state.wikidata.search(q, limit)
