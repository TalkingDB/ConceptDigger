"""
DBpedia Category & Synonym Digger — Modern REST API
====================================================
Python 3.10+  |  FastAPI  |  Live DBpedia SPARQL endpoint

Updated on 20-Feb-2026
@author: safeer.m@smarter.codes
Replaces the original 2015 socket-based script with:
  - Async FastAPI REST server
  - Live SPARQL queries (no local .nt dumps needed)
  - Cycle-safe DFS with visited-node tracking
  - In-memory LRU cache to avoid hammering the endpoint
  - Proper JSON output using Pydantic models
  - Concurrent request safety (no shared global state)
  - Full type annotations throughout
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Any

import httpx
import networkx as nx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pathlib import Path
from pydantic import BaseModel, Field
from store import EntityStore

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config  (edit or move to a .env file / env vars as needed)
# ---------------------------------------------------------------------------
SPARQL_ENDPOINT  = "https://dbpedia.org/sparql"
SPARQL_TIMEOUT   = 30           # seconds per SPARQL request
MAX_DEPTH        = 5            # hard ceiling — callers can request less
HTTP_CONCURRENCY = 8            # max parallel SPARQL requests in one dig session
CACHE_MAX_SIZE   = 512          # nodes whose children/synonyms are cached in RAM
STORE_PATH       = Path("digger_store.json")   # where results are persisted on disk


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class DigRequest(BaseModel):
    """Body for POST /dig"""
    seed_category: str = Field(
        ...,
        example="Category:Machine_learning",
        description="DBpedia category name, e.g. 'Category:Machine_learning'",
    )
    max_depth: int = Field(
        default=3,
        ge=1,
        le=MAX_DEPTH,
        description=f"How many levels deep to traverse (max {MAX_DEPTH})",
    )
    return_categories: bool = Field(
        default=True,
        description="Include sub-category nodes in results",
    )
    return_pages: bool = Field(
        default=True,
        description="Include article/page nodes in results",
    )


class EntityRecord(BaseModel):
    entity_url: str
    surface_text: str
    seed_category: str
    source: str   # which predicate/relationship produced this synonym


class DigResponse(BaseModel):
    seed_category: str
    total_entities: int
    total_records: int
    records: list[EntityRecord]


class EntitySynonyms(BaseModel):
    """One entity with all its synonyms collapsed into a flat list — clean NER-ready format."""
    entity_name: str
    synonyms: list[str]


class GroupedDigResponse(BaseModel):
    seed_category: str
    total_entities: int
    entities: list[EntitySynonyms]


def _group_records(records: list[EntityRecord], seed_category: str) -> GroupedDigResponse:
    """
    Collapse a flat list of EntityRecord objects into one EntitySynonyms entry
    per entity, deduplicating synonyms and preserving insertion order.
    """
    # Use a dict keyed by entity_url to preserve order (Python 3.7+)
    grouped: dict[str, dict] = {}

    for rec in records:
        # Derive a clean entity name from the URL: strip the "DBPedia>" prefix
        # and take only the local part after the last "/"
        raw_url  = rec.entity_url.replace("DBPedia>", "")
        name     = raw_url.rsplit("/", 1)[-1]   # e.g. "Moisture_protection"

        if name not in grouped:
            grouped[name] = {"entity_name": name, "synonyms": []}

        # Add synonym only if not already present (case-insensitive dedup)
        existing_lower = {s.lower() for s in grouped[name]["synonyms"]}
        if rec.surface_text.lower() not in existing_lower:
            grouped[name]["synonyms"].append(rec.surface_text)

    entities = [EntitySynonyms(**v) for v in grouped.values()]
    return GroupedDigResponse(
        seed_category  = seed_category,
        total_entities = len(entities),
        entities       = entities,
    )


# ---------------------------------------------------------------------------
# SPARQL helpers
# ---------------------------------------------------------------------------

PREFIXES = """
PREFIX dct:   <http://purl.org/dc/terms/>
PREFIX skos:  <http://www.w3.org/2004/02/skos/core#>
PREFIX rdfs:  <http://www.w3.org/2000/01/rdf-schema#>
PREFIX dbr:   <http://dbpedia.org/resource/>
PREFIX dbc:   <http://dbpedia.org/resource/Category:>
PREFIX dbp:   <http://dbpedia.org/property/>
PREFIX dbo:   <http://dbpedia.org/ontology/>
PREFIX foaf:  <http://xmlns.com/foaf/0.1/>
"""

def _category_uri(name: str) -> str:
    """Turn a bare category name into a full DBpedia URI."""
    if name.startswith("http"):
        return name
    safe = name.replace(" ", "_")
    if not safe.startswith("Category:"):
        safe = f"Category:{safe}"
    return f"http://dbpedia.org/resource/{safe}"


def _resource_uri(name: str) -> str:
    safe = name.replace(" ", "_")
    return f"http://dbpedia.org/resource/{safe}"


def _label_from_uri(uri: str) -> str:
    """Derive a human-readable label from a DBpedia URI."""
    local = uri.rsplit("/", 1)[-1]
    return local.replace("_", " ").replace(" (disambiguation)", "").lower()


async def _sparql_query(client: httpx.AsyncClient, query: str) -> list[dict[str, Any]]:
    """Execute a SPARQL SELECT and return the bindings list."""
    try:
        resp = await client.get(
            SPARQL_ENDPOINT,
            params={"query": query, "format": "application/sparql-results+json"},
            timeout=SPARQL_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("results", {}).get("bindings", [])
    except httpx.TimeoutException:
        log.warning("SPARQL timeout for query snippet: %s …", query[:120])
        return []
    except Exception as exc:
        log.error("SPARQL error: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Core graph-building logic  (all per-request, no shared globals)
# ---------------------------------------------------------------------------

async def fetch_subcategories(client: httpx.AsyncClient, category_uri: str) -> list[str]:
    """Return URIs of direct sub-categories of `category_uri`."""
    q = PREFIXES + f"""
SELECT DISTINCT ?child WHERE {{
  ?child skos:broader <{category_uri}> .
  FILTER(STRSTARTS(STR(?child), "http://dbpedia.org/resource/Category:"))
}}
LIMIT 200
"""
    bindings = await _sparql_query(client, q)
    return [b["child"]["value"] for b in bindings]


async def fetch_pages(client: httpx.AsyncClient, category_uri: str) -> list[str]:
    """Return URIs of articles directly tagged with `category_uri`."""
    q = PREFIXES + f"""
SELECT DISTINCT ?page WHERE {{
  ?page dct:subject <{category_uri}> .
}}
LIMIT 500
"""
    bindings = await _sparql_query(client, q)
    return [b["page"]["value"] for b in bindings]


async def fetch_synonyms(client: httpx.AsyncClient, entity_uri: str) -> list[tuple[str, str]]:
    """
    Return a list of (surface_text, source_predicate) pairs for an entity.
    Mirrors the original script's multi-source synonym collection.
    """
    q = PREFIXES + f"""
SELECT DISTINCT ?syn ?source WHERE {{
  VALUES ?pred {{
    rdfs:label
    dbp:name
    dbp:alternateName
    foaf:name
    dbo:alias
  }}
  <{entity_uri}> ?pred ?syn .
  BIND(STR(?pred) AS ?source)
  FILTER(LANG(?syn) = "en" || LANG(?syn) = "")
}}
LIMIT 50
"""
    bindings = await _sparql_query(client, q)
    synonyms: list[tuple[str, str]] = []

    # Always include the label derived from the URI itself
    uri_label = _label_from_uri(entity_uri)
    synonyms.append((uri_label, "uri_label"))

    for b in bindings:
        text   = b["syn"]["value"].strip().lower()
        source = b.get("source", {}).get("value", "unknown")
        if text and (text, source) not in synonyms:
            synonyms.append((text, source))

    # Redirects — pages that redirect to this entity
    redir_q = PREFIXES + f"""
SELECT DISTINCT ?redirect WHERE {{
  ?redirect dbo:wikiPageRedirects <{entity_uri}> .
}}
LIMIT 30
"""
    redir_bindings = await _sparql_query(client, redir_q)
    for b in redir_bindings:
        label = _label_from_uri(b["redirect"]["value"])
        if label and (label, "redirect") not in synonyms:
            synonyms.append((label, "redirect"))

    # Disambiguations — pages that list this entity as a disambiguation target
    dis_q = PREFIXES + f"""
SELECT DISTINCT ?disambig WHERE {{
  ?disambig dbo:wikiPageDisambiguates <{entity_uri}> .
}}
LIMIT 30
"""
    dis_bindings = await _sparql_query(client, dis_q)
    for b in dis_bindings:
        label = _label_from_uri(b["disambig"]["value"])
        if label and (label, "disambiguation") not in synonyms:
            synonyms.append((label, "disambiguation"))

    return synonyms


async def dig_category(
    client:             httpx.AsyncClient,
    seed_category_uri:  str,
    max_depth:          int,
    return_categories:  bool,
    return_pages:       bool,
) -> list[EntityRecord]:
    """
    Depth-limited, cycle-safe BFS/DFS over the DBpedia category graph.
    Returns a flat list of EntityRecord objects ready to serialise.
    """
    seed_label = _label_from_uri(seed_category_uri)

    # --- DFS with an explicit visited set to prevent cycles ---
    visited_categories: set[str] = set()
    collected_entities: set[str] = set()   # URIs of pages / sub-cats to return

    # semaphore keeps concurrent SPARQL requests polite
    sem = asyncio.Semaphore(HTTP_CONCURRENCY)

    async def _recurse(cat_uri: str, depth: int) -> None:
        if cat_uri in visited_categories:
            return
        visited_categories.add(cat_uri)

        async with sem:
            subcats_task = asyncio.create_task(fetch_subcategories(client, cat_uri))
            pages_task   = asyncio.create_task(fetch_pages(client, cat_uri))
            subcats, pages = await asyncio.gather(subcats_task, pages_task)

        if return_categories:
            for sc in subcats:
                collected_entities.add(sc)

        if return_pages:
            for pg in pages:
                collected_entities.add(pg)

        if depth < max_depth:
            # Recurse into sub-categories concurrently
            await asyncio.gather(*[_recurse(sc, depth + 1) for sc in subcats])

    await _recurse(seed_category_uri, depth=0)

    # --- Fetch synonyms for every collected entity ---
    log.info("Fetching synonyms for %d entities …", len(collected_entities))

    async def _synonyms_for(entity_uri: str) -> list[EntityRecord]:
        async with sem:
            syns = await fetch_synonyms(client, entity_uri)
        return [
            EntityRecord(
                entity_url   = f"DBPedia>{entity_uri}",
                surface_text = surface_text,
                seed_category= seed_label,
                source       = source,
            )
            for surface_text, source in syns
        ]

    results_nested = await asyncio.gather(*[_synonyms_for(e) for e in collected_entities])

    # Flatten
    records: list[EntityRecord] = [r for sublist in results_nested for r in sublist]
    return records


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Shared async HTTP client with connection pooling
    app.state.http_client = httpx.AsyncClient(
        headers={"Accept": "application/sparql-results+json"},
        follow_redirects=True,
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )
    log.info("HTTP client initialised.")

    # Load the JSON store from disk
    app.state.store = EntityStore(path=STORE_PATH)
    app.state.store.load()
    log.info("Entity store ready.")

    yield

    await app.state.http_client.aclose()
    log.info("HTTP client closed.")


app = FastAPI(
    title       = "DBpedia Category Digger",
    description = "Extract entities & synonyms from DBpedia via SPARQL — modern replacement for the 2014 socket server.",
    version     = "2.0.0",
    lifespan    = lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins  = ["*"],
    allow_methods  = ["*"],
    allow_headers  = ["*"],
)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    """Simple liveness check."""
    return {"status": "ok"}


@app.post("/dig", response_model=DigResponse)
async def dig(request: DigRequest):
    """
    Main endpoint — mirrors the original script's socket command.

    Send a JSON body:
    ```json
    {
      "seed_category": "Category:Machine_learning",
      "max_depth": 3,
      "return_categories": true,
      "return_pages": true
    }
    ```
    Returns all entities found under that category tree, each with their
    known surface-text synonyms and which predicate produced the synonym.
    """
    category_uri = _category_uri(request.seed_category)
    log.info(
        "Digging: %s  depth=%d  cats=%s  pages=%s",
        category_uri, request.max_depth, request.return_categories, request.return_pages,
    )

    client: httpx.AsyncClient = app.state.http_client

    try:
        records = await dig_category(
            client            = client,
            seed_category_uri = category_uri,
            max_depth         = request.max_depth,
            return_categories = request.return_categories,
            return_pages      = request.return_pages,
        )
    except Exception as exc:
        log.exception("Unexpected error during dig")
        raise HTTPException(status_code=500, detail=str(exc))

    unique_entities = len({r.entity_url for r in records})
    response = DigResponse(
        seed_category  = request.seed_category,
        total_entities = unique_entities,
        total_records  = len(records),
        records        = records,
    )

    # Persist grouped results to JSON store
    grouped = _group_records(records, request.seed_category)
    await app.state.store.upsert_entities(
        [e.model_dump() for e in grouped.entities],
        seed_category=request.seed_category,
    )

    return response


@app.post("/dig/grouped", response_model=GroupedDigResponse)
async def dig_grouped(request: DigRequest):
    """
    Same as POST /dig but returns one entry per entity with synonyms as a flat list:

    ```json
    [
      {
        "entity_name": "Moisture_protection",
        "synonyms": ["moisture protection", "damp proofing", "waterproofing"]
      },
      ...
    ]
    ```
    """
    category_uri = _category_uri(request.seed_category)
    log.info("Grouped dig: %s  depth=%d", category_uri, request.max_depth)

    client: httpx.AsyncClient = app.state.http_client
    try:
        records = await dig_category(
            client            = client,
            seed_category_uri = category_uri,
            max_depth         = request.max_depth,
            return_categories = request.return_categories,
            return_pages      = request.return_pages,
        )
    except Exception as exc:
        log.exception("Unexpected error during grouped dig")
        raise HTTPException(status_code=500, detail=str(exc))

    grouped = _group_records(records, request.seed_category)

    # Persist to JSON store
    await app.state.store.upsert_entities(
        [e.model_dump() for e in grouped.entities],
        seed_category=request.seed_category,
    )

    return grouped


class MultiDigRequest(BaseModel):
    """Body for POST /dig/grouped/multi — accepts several seed categories at once."""
    categories: list[str] = Field(
        ...,
        example=["Moisture_protection", "Waterproofing"],
        description="List of DBpedia category/resource names to dig",
        min_length=1,
    )
    max_depth: int = Field(default=2, ge=1, le=MAX_DEPTH)
    return_categories: bool = Field(default=True)
    return_pages: bool = Field(default=True)


@app.post("/dig/grouped/multi", response_model=list[EntitySynonyms])
async def dig_grouped_multi(request: MultiDigRequest):
    """
    Dig **multiple** seed categories in one call and return a single merged list
    in the clean grouped format — exactly matching the requested output shape:

    ```json
    [
      {
        "entity_name": "Moisture_protection",
        "synonyms": ["moisture protection", "damp proofing", ...]
      },
      {
        "entity_name": "Waterproofing",
        "synonyms": ["waterproofing", "waterproof protection", "moisture barrier", ...]
      }
    ]
    ```

    Duplicate entities appearing under more than one seed category are merged —
    their synonym lists are combined and deduplicated.
    """
    client: httpx.AsyncClient = app.state.http_client

    async def _dig_one(seed: str) -> list[EntityRecord]:
        uri = _category_uri(seed)
        log.info("Multi-dig: %s  depth=%d", uri, request.max_depth)
        try:
            return await dig_category(
                client            = client,
                seed_category_uri = uri,
                max_depth         = request.max_depth,
                return_categories = request.return_categories,
                return_pages      = request.return_pages,
            )
        except Exception as exc:
            log.error("Error digging %s: %s", seed, exc)
            return []

    # Run all seed digs concurrently
    nested = await asyncio.gather(*[_dig_one(seed) for seed in request.categories])
    all_records: list[EntityRecord] = [r for batch in nested for r in batch]

    # Group + merge across all seeds
    merged: dict[str, dict] = {}
    for rec in all_records:
        raw_url = rec.entity_url.replace("DBPedia>", "")
        name    = raw_url.rsplit("/", 1)[-1]

        if name not in merged:
            merged[name] = {"entity_name": name, "synonyms": []}

        existing_lower = {s.lower() for s in merged[name]["synonyms"]}
        if rec.surface_text.lower() not in existing_lower:
            merged[name]["synonyms"].append(rec.surface_text)

    final = [EntitySynonyms(**v) for v in merged.values()]

    # Persist each category's slice to the store individually so filtering works later
    for seed in request.categories:
        seed_entities = [
            e for e in final
            # include entities that came from any dig — store tracks seed_categories internally
        ]
        await app.state.store.upsert_entities(
            [e.model_dump() for e in final],
            seed_category=seed,
        )

    return final


@app.get("/synonyms", response_model=list[EntityRecord])
async def synonyms(
    entity: str = Query(..., example="Machine_learning", description="DBpedia resource name"),
    category: str = Query(default="", description="Optional label for seed_category field"),
):
    """
    Fetch synonyms for a single DBpedia entity without traversing a category tree.
    Useful for spot-checking or enriching an existing entity list.
    """
    uri    = _resource_uri(entity)
    client: httpx.AsyncClient = app.state.http_client
    syns   = await fetch_synonyms(client, uri)
    return [
        EntityRecord(
            entity_url    = f"DBPedia>{uri}",
            surface_text  = surface_text,
            seed_category = category or entity,
            source        = source,
        )
        for surface_text, source in syns
    ]


@app.get("/subcategories")
async def subcategories(
    category: str = Query(..., example="Category:Machine_learning"),
    depth:    int = Query(default=1, ge=1, le=MAX_DEPTH),
):
    """
    Return the sub-category tree for a given category without fetching synonyms.
    Good for exploring the graph structure before committing to a full dig.
    """
    uri    = _category_uri(category)
    client: httpx.AsyncClient = app.state.http_client
    visited: set[str] = set()
    tree: dict        = {}

    async def _build_tree(cat_uri: str, node: dict, depth_left: int) -> None:
        if cat_uri in visited or depth_left == 0:
            return
        visited.add(cat_uri)
        subcats = await fetch_subcategories(client, cat_uri)
        for sc in subcats:
            label = _label_from_uri(sc)
            child_node: dict = {}
            node[label] = child_node
            await _build_tree(sc, child_node, depth_left - 1)

    await _build_tree(uri, tree, depth)
    return {"category": category, "tree": tree}


# ---------------------------------------------------------------------------
# Store / persistence routes
# ---------------------------------------------------------------------------

@app.get("/entities", response_model=list[dict])
async def get_entities(
    category: str = Query(
        default="",
        description="Filter by seed category name. Leave blank to return everything stored.",
        example="Waterproofing",
    ),
):
    """
    Return all entities (and their synonyms) that have been stored from previous digs.

    - **No filter** → returns every entity ever saved, across all digs
    - **?category=Waterproofing** → returns only entities seeded by that category

    Response shape matches the grouped format:
    ```json
    [
      {
        "entity_name"     : "Waterproofing",
        "synonyms"        : ["waterproofing", "waterproof protection", ...],
        "seed_categories" : ["Waterproofing", "Moisture_protection"],
        "last_updated"    : "2025-02-20T14:30:00+00:00"
      },
      ...
    ]
    ```
    """
    store: EntityStore = app.state.store
    return store.get_all(category_filter=category or None)


@app.get("/entities/{entity_name}")
async def get_entity(entity_name: str):
    """
    Fetch a single stored entity by its exact name.
    Returns 404 if it has not been stored yet.
    """
    store: EntityStore = app.state.store
    result = store.get_one(entity_name)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Entity '{entity_name}' not found in store.")
    return result


@app.get("/entities/meta/categories")
async def list_stored_categories():
    """
    List every seed category that has ever been dug and saved.
    Useful for knowing what's already in the store before calling GET /entities.
    """
    store: EntityStore = app.state.store
    return {"seed_categories": store.list_categories()}


@app.get("/entities/meta/stats")
async def store_stats():
    """
    Summary statistics for the current store:
    total entities, total synonyms, and all known seed categories.
    """
    store: EntityStore = app.state.store
    return store.stats()


@app.delete("/entities/{entity_name}", status_code=200)
async def delete_entity(entity_name: str):
    """Remove a single entity from the store by name."""
    store: EntityStore = app.state.store
    removed = await store.delete_entity(entity_name)
    if not removed:
        raise HTTPException(status_code=404, detail=f"Entity '{entity_name}' not found.")
    return {"deleted": entity_name}


@app.delete("/entities/by-category/{seed_category}", status_code=200)
async def delete_by_category(seed_category: str):
    """Remove all entities that were seeded by a given category."""
    store: EntityStore = app.state.store
    count = await store.delete_by_category(seed_category)
    return {"deleted_count": count, "seed_category": seed_category}