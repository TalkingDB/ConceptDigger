Concept Digger
====================

A category/page digger & synonym finder over DBPedia, rebuilt from a
10-year-old Python 2 raw-socket service into a FastAPI application backed by
**live SPARQL hydration** instead of bulk local dump files.

## What changed from the original

| Original | Now |
|---|---|
| Python 2.7, raw TCP socket on port 5007 | Python 3.12, FastAPI/uvicorn HTTP API on port 5007 |
| Graph built by bulk-loading 6 local `.nt` dump files at startup (or unpickled from disk) | Graph boots **empty**. The first `/dig` naming an unseen category hydrates it from SPARQL on the spot, then caches it — later digs on it are served entirely from memory |
| Data source: static DBPedia dump files (`debug_data/`, not shipped in this repo) | Data source: a configurable SPARQL endpoint (default `https://dbpedia.org/sparql`) — point it at a self-hosted Virtuoso instance later via an env var, no code changes |
| Single `/dig`-equivalent behavior, with a real bug (see below) | Two endpoints: `/api/v1/dig` (bug preserved) and `/api/v1/dig/fixed` (corrected) |
| No concurrency safety, no rate limiting | Async, lock-guarded graph writes, de-duped hydration, and hard caps on depth + SPARQL calls per request |
| `insert-nodes`, `assign-synonym-from-*` file-ingestion endpoints | `hydrate-category`, `hydrate-synonyms` — same idea, SPARQL-backed |
| `wikipedia_bold_to_ntriple_file.py` (bold-keyword miner) | Kept as-is, ported to Python 3 syntax only. Still standalone, still not wired into the app |

## The preserved bug

In the original `prepareOutput()`, every output record's `seed_category` was
set from the **matched node's own name**, not the seed category that was
actually dug — so results all claim to be their own seed. `/api/v1/dig`
reproduces this exactly. `/api/v1/dig/fixed` is the same traversal with
correct attribution, added as a separate endpoint rather than a fix-in-place.

## API

### `POST /api/v1/dig` and `POST /api/v1/dig/fixed`

Request body — a batch of digs, same shape as the original protocol:

```json
[
  ["Category:Birds", 3, 1, 1],
  ["Category:Fish", 2, 0, 1]
]
```

Each item is `[seed_category, max_depth, return_categories, return_pages]`
(the last two are `0`/`1`). `max_depth` is clamped to `MAX_DEPTH_HARD_CAP`
regardless of what's requested.

Response — a JSON array (not the legacy concatenated-string format):

```json
[
  {
    "entity_url": "DBPedia>Category:Sparrows",
    "surface_text": "sparrows",
    "seed_category": "Category:Sparrows",
    "how_this_record": "category_uri"
  }
]
```

`seed_category` is the matched node's own name on `/dig` (bug preserved),
and the actual dug seed on `/dig/fixed`.

If the per-request SPARQL budget runs out before every branch could be fully
explored/enriched, the response header `X-SPARQL-Budget-Exhausted: true` is
set. The body is always a plain JSON array either way — whatever was already
hydrated (now or previously) is what you get back.

### `POST /api/v1/hydrate-category`

Pre-warm a category's structure (subcategories + member articles) without
waiting for a `/dig` to trigger it.

```json
{ "category": "Category:Birds", "depth": 2 }
```

### `POST /api/v1/hydrate-synonyms`

Pre-warm labels/alt-names/redirects/disambiguations for one entity.

```json
{ "entity": "Category:Birds" }
```

### `GET /api/v1/health`

Returns `{"status": "ok", "graph_nodes": N, "graph_edges": M}`.

## Configuration (env vars)

| Var | Default | Meaning |
|---|---|---|
| `SPARQL_ENDPOINT` | `https://dbpedia.org/sparql` | Where hydration queries go |
| `MAX_DEPTH_HARD_CAP` | `6` | Absolute ceiling on `max_depth`, regardless of request |
| `MAX_SPARQL_CALLS_PER_REQUEST` | `10000` | Shared SPARQL-call budget across an entire `/dig` batch |
| `SPARQL_TIMEOUT_SECONDS` | `15` | Per-call HTTP timeout |
| `CACHE_FILE_PATH` | `data/wiki_data.json` | Where the hydrated graph is persisted as JSON |
| `PORT` | `5007` | HTTP port |

## Running it

```bash
./start.sh
```

Builds the image, runs it on port 5007, and mounts `./data` so the JSON
graph cache survives container restarts.

To run without Docker:

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 5007
```

## Concurrency & safety notes

- Runs as a standard single-worker FastAPI/uvicorn async app. Graph writes
  are guarded by an `asyncio.Lock`; concurrent hydration requests for the
  *same* category/entity are de-duped so they share one set of SPARQL calls
  instead of double-firing them.
- Scaling to multiple uvicorn workers/processes would need a shared cache
  backend (e.g. Redis) instead of the in-process graph — out of scope here,
  called out for later.
- `wikipedia_bold_to_ntriple_file.py` is unrelated to the live hydration
  path; it's an offline tool for mining a raw Wikipedia XML dump. Its one
  extra dependency (`dewiki`) lives in `requirements-tools.txt`, not the
  app's own `requirements.txt`.

## License

AGPL — see `LICENCE-AGPL-3.0.txt`.
