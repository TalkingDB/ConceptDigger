"""
FastAPI application entrypoint.

Graph state, the shared httpx client, and the hydration service are created
once at startup (lifespan) and stashed on app.state, so every request
handler shares the same in-memory graph and connection pool.

Run directly with:
    uvicorn app.main:app --host 0.0.0.0 --port 5007

"""
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse

logging.getLogger("app").setLevel(logging.INFO)
if not logging.getLogger("app").handlers:
    logging.getLogger("app").addHandler(logging.StreamHandler())


from .config import settings
from .export import list_export_files
from .graph_store import GraphStore
from .hydration import HydrationService
from .routers import dig, health, hydrate, graph, graph_data, synonyms
from .routers.synonyms import patch_openapi_file_enum
from .sparql_client import SparqlTransientError, sparql_client


@asynccontextmanager
async def lifespan(app: FastAPI):
    store = GraphStore(settings.cache_file_path)
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(settings.sparql_timeout_seconds, connect=10.0, pool=10.0),
    )
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
app.include_router(synonyms.router)


def export_names_for_docs() -> list[str]:
    """File names for the Swagger dropdown. Newest first, from the running store."""
    store = getattr(app.state, "store", None)
    if store is None:
        return []
    return list_export_files(store)


def custom_openapi():
    # Rebuilt on each fetch so the file_name dropdown includes exports written
    # after the process started.
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    patch_openapi_file_enum(schema, export_names_for_docs())
    return schema


app.openapi = custom_openapi


@app.exception_handler(SparqlTransientError)
async def sparql_transient(_: Request, exc: SparqlTransientError):
    return JSONResponse(status_code=502, content={"detail": str(exc)})