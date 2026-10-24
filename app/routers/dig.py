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

Response body:
{
    "data": [
        {
            "entity_url": "DBPedia>Sodium_chloride",
            "surface_text": "sodium chloride",
            "seed_category": "Category:Example",
            "how_this_record": "rdfs:label"
        }
    ],
    "status": "complete",
    "failed_requests": 0,
    "error": null
}

Status values:
- complete: All relevant SPARQL requests succeeded.
- incomplete: Some SPARQL requests failed, but results are available.
- failed: All relevant attempted SPARQL requests failed.

When the SPARQL budget is exhausted, any available results are returned,
and X-SPARQL-Budget-Exhausted: true is set in the response headers.
"""
from typing import Literal, List

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
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

class DigResultItem(BaseModel):
    entity_url: str
    surface_text: str
    seed_category: str
    how_this_record: str


class DigResponse(BaseModel):
    data: List[DigResultItem] = Field(
        default_factory=list,
        description="Retrieved entities and their surface forms.",
    )
    status: Literal["complete", "incomplete", "failed"] = Field(
        description="Whether the retrieval completed successfully.",
    )
    failed_requests: int = Field(
        ge=0,
        description="Number of SPARQL requests that failed.",
    )
    error: str | None = Field(
        default=None,
        description="Error message when retrieval failed or was incomplete.",
    )


@router.post(
    "/dig", 
    response_model=DigResponse,
    responses={
        502: {
            "model": DigResponse,
            "description": "All relevant attempted SPARQL requests failed."
        }
    }
)
async def dig(body: DigRequest, request: Request, response: Response) -> DigResponse | JSONResponse:
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

    result = await run_dig(hydration, store, items, budget, fixed=True)
   
    # validate the result returned by run_dig against the API schema.
    dig_response = DigResponse.model_validate(result)

    headers = {}
    if budget.exhausted:
        headers["X-SPARQL-Budget-Exhausted"] = "true"

    if dig_response.status == "failed":
        return JSONResponse(
            status_code=502,
            content=dig_response.model_dump(mode="json"),
            headers=headers,
        )

    for name, value in headers.items():
        response.headers[name] = value

    return dig_response

@router.post("/export")
async def export(body: ExportRequest, request: Request):
    store = request.app.state.store
    max_depth = require_max_depth(body.max_depth)
    seed = require_category(body.category)
    if not store.has_node(seed):
        raise HTTPException(status_code=404, detail=f"Category not in cache: {seed}")
    records = collect_from_cache(store, seed, max_depth, return_categories=True, return_pages=True)
    return {"files": await asyncio.to_thread(write_exports, store, [seed], records)}
