"""
GET /api/v1/graph/data

JSON twin of GET /api/v1/graph (which returns HTML). Same rules: NEVER
hydrates and NEVER calls SPARQL, it only reads what is already in the
in-memory GraphStore. Used by the knowledge-source-api gateway.

Query params:
    root  - category local name, e.g. "Category:Food_ingredients"
    depth - levels of cached children to walk (clamped to MAX_DEPTH_HARD_CAP)

Response: {"root", "depth", "nodes": [...], "edges": [{"source","target"}]}
If the root isn't cached, "nodes" is empty and a "note" explains why.
"""
from fastapi import APIRouter, Query, Request

from ..config import settings
from .graph import _build_graph_data

router = APIRouter(prefix="/api/v1", tags=["graph"])


@router.get("/graph/data")
async def get_graph_data(
    request: Request,
    root: str = Query(..., description="Category local name, e.g. 'Category:Food_ingredients'"),
    depth: int = Query(6, ge=0, description="How many levels of cached children to walk"),
):
    depth = min(depth, settings.max_depth_hard_cap)
    return _build_graph_data(store=request.app.state.store, root=root, depth=depth)