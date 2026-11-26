# Troubleshooting

[Docs home](README.md) › Troubleshooting

Problems seen when running or calling ConceptDigger, with the cause and the fix. Settings named here are described in the [README configuration table](../README.md#configuration).

| Symptom | Likely cause | Fix |
| :--- | :--- | :--- |
| `400` from `/api/v1/dig` | The category has unsafe characters, is not a category, or `max_depth` is above `MAX_DEPTH_HARD_CAP` (6) | Send a `Category:` name, a [DBpedia](https://www.dbpedia.org/about/) URL or a Wikipedia category URL; lower `max_depth` |
| `404` from `/api/v1/dig` | The seed category does not exist in DBpedia | Check the exact name on Wikipedia; DBpedia names use underscores, such as `Category:Food_ingredients` |
| `404` from `/api/v1/export` or `/api/v1/graph` | The category or root is not in the cache yet | Run a dig (or `hydrate-category`) for it first |
| `409` from `/api/v1/graph` | Another call for the same root is running | Wait and try again |
| `502` | The [SPARQL](https://www.w3.org/TR/sparql11-query/) endpoint failed for a call that could not be skipped | Retry later, or point `SPARQL_ENDPOINT` at a self hosted endpoint |
| Digs are slow | Every call is spaced by `SPARQL_MIN_INTERVAL_SECONDS` and retried on 429 and 5xx; the public endpoint is shared and often busy | Dig smaller trees, keep the cache, or use a self hosted endpoint |
| Wikipedia abstracts fail with 403 | `WIKIPEDIA_USER_AGENT` has no contact details | Keep a URL or email in the user agent |
| Results look partial, with no error | A lookup failed repeatedly and was skipped; the response is not always flagged as incomplete | Run the same dig again; nodes are only marked hydrated after every lookup succeeds, so missing parts are retried |
| New Wikipedia names never appear | The cache never expires | Delete the cache file (`CACHE_FILE_PATH`, default `data/graph_cache.json`) and dig again |
| Deep digs slow down as the cache grows | Each cache write saves the whole graph | Keep `PERSIST_INTERVAL_SECONDS` above 0 (default 30) |
| `data/exports/` keeps growing | Exports are pruned to the newest `EXPORT_MAX_FILES` (50); `0` turns pruning off | Set a positive value |
| Two instances return different results | Each process keeps its own in memory graph | Run one worker, or add a shared store |
| Port 5007 already in use | Another container is running | Stop it, or set `PORT` for `start.sh` |
| The cache is lost after a container restart | `./data` is not mounted | Use `./start.sh` or `docker compose up`, which mount it |

## Before you open an issue

Include the request body, the response code and body, the commit (`git rev-parse --short HEAD`), and whether you used the public DBpedia endpoint.

---

**Related:** [Concepts](concepts.md) · [Configuration](../README.md#configuration) · [Known limitations](../README.md#known-limitations)
