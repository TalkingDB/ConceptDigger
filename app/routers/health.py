from fastapi import APIRouter, Request

router = APIRouter(tags=["health"])


@router.get("/api/v1/health")
async def health(request: Request):
    store = request.app.state.store
    return {
        "status": "ok",
        "graph_nodes": store.graph.number_of_nodes(),
        "graph_edges": store.graph.number_of_edges(),
    }
