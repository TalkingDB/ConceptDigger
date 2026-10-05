"""
FastAPI application entrypoint.

Graph state, the shared httpx client, and the hydration service are created
once at startup (lifespan) and stashed on app.state, so every request
handler shares the same in-memory graph and connection pool.

Run directly with:
    uvicorn app.main:app --host 0.0.0.0 --port 5007

"""
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from .config import settings
from .graph_store import GraphStore
from .hydration import HydrationService
from .routers import dig, health, hydrate, graph, graph_data
from .sparql_client import sparql_client


@asynccontextmanager
async def lifespan(app: FastAPI):
    store = GraphStore(settings.cache_file_path)
    http_client = httpx.AsyncClient()
    hydration = HydrationService(store, sparql_client, http_client)

    app.state.store = store
    app.state.hydration = hydration
    app.state.http_client = http_client

    yield

    await http_client.aclose()


app = FastAPI(
    title="ConceptDigger",
    description="DBPedia category/page digger & synonym finder, backed by live SPARQL hydration.",
    version="2.0.0",
    lifespan=lifespan,
)

app.include_router(dig.router)
app.include_router(hydrate.router)
app.include_router(health.router)
app.include_router(graph.router)
app.include_router(graph_data.router)