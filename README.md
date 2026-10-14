# ConceptDigger

A REST service that digs a DBpedia category tree and returns every category and
article under it, together with the surface-text synonyms for each one. The
output is used as training data for Named Entity Recognition (NER) models.

ConceptDigger replaces a 2015 Python 2 raw-socket service. Instead of loading
huge DBpedia dump files into RAM at startup, it queries a live SPARQL endpoint
on demand and caches what it learns.

## How it works

1. **Normalize** - every category you send (DBpedia URL, Wikipedia URL, or a
   `Category:` name) is turned into a graph key such as
   `Category:Food_ingredients`. Unsafe characters are rejected.
2. **Check** - one SPARQL `ASK` confirms the category exists. Unknown seeds
   return `404`.
3. **Hydrate structure** - for any category not seen before, two SPARQL calls
   fetch its sub-categories (`skos:broader`) and member articles
   (`dct:subject`). The result is stored in an in-memory graph.
4. **Walk** - the cached graph is traversed down to `max_depth`, collecting
   categories and/or pages according to the flags in the request.
5. **Hydrate synonyms** - for every collected node, four SPARQL calls fetch
   labels (`rdfs:label`), alternate names (`dbo:alias`, `dbp:name`,
   `dbp:alternateName`, `foaf:name`), redirects and disambiguations.
6. **Respond** - one record per (node, synonym).

The graph is saved to a JSON file after each hydration step, so a repeat dig
over the same categories needs no SPARQL calls at all, and the cache survives
restarts.

### Depth

`max_depth: 0` returns only the direct children of the seed. Each extra level
descends one more level of sub-categories. The traversal always descends into
sub-categories, even when `return_categories` is `false`, because pages can
live deeper down.

### Graph model

- Nodes are DBpedia local names (`Category:Birds`, `Chicken`).
- Edges point parent -> child (broader category -> narrower category or article).
- Each node carries its synonym list plus two flags, `structure_hydrated` and
  `synonyms_hydrated`. A node is only flagged once all of its lookups
  completed, so partially fetched nodes are retried on a later dig.

## API

Base path: `/api/v1`. Interactive docs: `http://localhost:5007/docs`.

### `POST /api/v1/dig`

```json
{
  "categories": ["http://dbpedia.org/resource/Category:Food_ingredients"],
  "max_depth": 1,
  "return_categories": true,
  "return_pages": true
}
```

| Field | Type | Default | Notes |
|---|---|---|---|
| `categories` | list of strings | required | At least one. DBpedia URL, Wikipedia URL or `Category:` name |
| `max_depth` | int >= 0 | `1` | Must not exceed `MAX_DEPTH_HARD_CAP`, otherwise `400` |
| `return_categories` | bool | `true` | Include sub-categories in the output |
| `return_pages` | bool | `true` | Include member articles in the output |

`max_depth`, `return_categories` and `return_pages` apply to every category in
the request.

Accepted category formats:

- `http://dbpedia.org/resource/Category:Food_ingredients`
- `https://en.wikipedia.org/wiki/Category:Food_ingredients`
- `Category:Food_ingredients` or `Category:Food ingredients`

Response: a JSON array (abridged, illustrative):

```json
[
  {
    "entity_url": "DBPedia>Category:Spices",
    "surface_text": "spices",
    "seed_category": "Category:Food_ingredients",
    "how_this_record": "rdfs:label"
  },
  {
    "entity_url": "DBPedia>Category:Spices",
    "surface_text": "spice",
    "seed_category": "Category:Food_ingredients",
    "how_this_record": "redirect"
  }
]
```

| Field | Meaning |
|---|---|
| `entity_url` | `DBPedia>` followed by the node's local name |
| `surface_text` | A lowercase synonym for that node |
| `seed_category` | The category you dug that led to this node |
| `how_this_record` | Where the synonym came from: `rdfs:label`, `alt_name`, `redirect` or `disambiguation` |

The array is not ordered. A node reachable from several seeds appears once per
seed.

Status codes:

| Code | Meaning |
|---|---|
| `200` | Success (possibly partial, see below) |
| `400` | Invalid category, unsafe characters, or `max_depth` above the cap |
| `404` | A seed category does not exist in DBpedia |
| `502` | The SPARQL endpoint was unavailable for a call that could not be skipped |

If the per-request SPARQL budget runs out, the response still contains
everything hydrated so far and carries the header
`X-SPARQL-Budget-Exhausted: true`.

### `POST /api/v1/hydrate-category`

Pre-warms the structure (sub-categories and articles) of a category without
returning anything.

```json
{ "category": "Category:Birds", "max_depth": 2 }
```

Returns `category`, `max_depth`, `new_nodes`, `new_edges`,
`sparql_calls_used` and `budget_exhausted`.

### `POST /api/v1/hydrate-synonyms`

Pre-warms the synonyms of one category or article.

```json
{ "entity": "Category:Birds" }
```

Returns `entity`, `synonyms_added`, `sparql_calls_used` and `budget_exhausted`.

### `GET /api/v1/graph?root=Category:Birds&depth=6`

A self-contained HTML tree viewer for the cached graph (search, zoom, pan,
expand/collapse, synonym tooltips).

### `GET /api/v1/graph/data?root=Category:Birds&depth=6`

The same data as JSON: `{ root, depth, nodes[], edges[] }`. Used by the
knowledge-source-api gateway.

Both graph endpoints only read the cache. They never call SPARQL. If the root
has not been hydrated yet, the result is empty with a note; run
`/hydrate-category` or `/dig` first. `depth` is capped at
`MAX_DEPTH_HARD_CAP + 1`.

### `GET /api/v1/health`

```json
{ "status": "ok", "graph_nodes": 1234, "graph_edges": 1500 }
```

## Configuration

All settings are environment variables.

| Variable | Default | Meaning |
|---|---|---|
| `SPARQL_ENDPOINT` | `https://dbpedia.org/sparql` | Endpoint for all queries. Point it at a self-hosted Virtuoso instance if you have one |
| `MAX_DEPTH_HARD_CAP` | `6` | Highest `max_depth` a request may use |
| `MAX_SPARQL_CALLS_PER_REQUEST` | `10000` | SPARQL calls one request may make. After that, hydration stops and the header above is set |
| `SPARQL_TIMEOUT_SECONDS` | `20` | Read timeout per SPARQL call |
| `SPARQL_MIN_INTERVAL_SECONDS` | `0.5` | Minimum gap between outgoing calls, shared by the whole process |
| `SPARQL_MAX_RETRIES` | `3` | Retries on 429/502/503/504. Timeouts get one retry |
| `CACHE_FILE_PATH` | `data/graph_cache.json` | Where the graph cache is saved |
| `PORT` | `5007` | Host port used by `start.sh` (the container always listens on 5007) |

## Running it

With Docker:

```bash
./start.sh
```

This builds the image, starts the container `concept-digger` on port 5007 and
mounts `./data` so the cache survives restarts. Check it with:

```bash
curl http://localhost:5007/api/v1/health
```

For development with live reload (mounts `./app` and `./data`):

```bash
docker compose up --build
```

Without Docker (Python 3.12):

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 5007
```

Try a dig:

```bash
curl -X POST http://localhost:5007/api/v1/dig \
  -H "Content-Type: application/json" \
  -d '{"categories": ["Category:Food_ingredients"], "max_depth": 1}'
```

The first dig on a category is slow, because calls are throttled to about two
per second to stay under DBpedia's rate limits. Later digs on the same category
are served from the cache.

## Reliability and safety

- **Throttling and retries** - calls are spaced out and retried with backoff on
  429/502/503/504 (honouring `Retry-After`, capped at 30 s).
- **Call budget** - one request cannot trigger an unbounded crawl.
- **Skip-and-continue** - if a single lookup keeps failing, it is skipped, the
  node stays un-hydrated so it is retried next time, and the dig continues.
- **De-duplicated hydration** - concurrent requests for the same node share one
  set of SPARQL calls.
- **Safe writes** - graph updates are lock-guarded and the cache file is
  written via a temp file and rename.
- **Input validation** - category names that could alter a SPARQL query are
  rejected before any query is built.

## Project structure

```
app/
  main.py            FastAPI app and startup wiring
  config.py          Environment-driven settings
  category.py        Input normalization and validation
  dig.py             Traversal and output building for /dig
  hydration.py       SPARQL budget and hydration of structure and synonyms
  sparql_client.py   Async SPARQL client with throttling and retries
  graph_store.py     In-memory graph, JSON cache, de-duplication
  utils.py           Label conversion and name safety checks
  routers/           dig, hydrate, graph, graph_data, health
tests/               unit + functional API tests, fixtures, golden files
Dockerfile
docker-compose.yml
start.sh
```

## Tests

Functional tests talk to the FastAPI app with a mocked SPARQL client and
fixtures in `tests/test_data/`. They never call live DBpedia.

```bash
pip install -r requirements-dev.txt
pytest tests/
```

Run artifacts (JUnit XML, HTML report, JSON summary) are written to
`tests/records/`. Expected `/dig` and `/graph/data` payloads are stored in
`tests/test_data/golden/`.

GitHub Actions runs `pytest tests/` on every pull request and every push to
`main`/`master`. Mark the `pytest` check as required in branch protection.

## Differences from the legacy service

| Legacy | Now |
|---|---|
| Python 2, raw TCP socket | Python 3.12, HTTP API (FastAPI) |
| Request: `[["Category:Birds", 3, 1, 1], ...]` with a per-category depth and flags | Request: JSON object with `categories`, `max_depth`, `return_categories`, `return_pages` shared by all categories |
| Response: concatenated JSON strings | Response: a JSON array |
| `seed_category` was the matched node itself (a bug) | `seed_category` is the category that was actually dug |
| Depth silently clamped | Depth above the cap returns `400` |
| Bulk `.nt` dump files held in RAM, pickled to disk | Live SPARQL with a JSON cache |
| `insert-nodes`, `assign-synonym-from-*` | `/hydrate-category`, `/hydrate-synonyms` |
| One global result list shared by all requests | Per-request state |
| Bold-keyword mining from Wikipedia dumps | Removed (still in git history) |

Clients that used the old socket protocol, such as the Training Portal, must be
updated to the new request and response format.

## Known limitations

- The cache never expires, so DBpedia changes are not picked up automatically.
  Delete the cache file to rebuild it.
- Runs as a single worker. Several workers would each keep their own graph; a
  shared store such as Redis would be needed to scale out.
- If lookups are skipped after repeated failures, the response is not flagged
  as incomplete (only budget exhaustion sets a header).
- The public DBpedia endpoint can be slow or rate-limited; a self-hosted
  endpoint is recommended for heavy use.
- No authentication.

## License

GNU Affero General Public License v3. See `LICENCE-AGPL-3.0.txt`.