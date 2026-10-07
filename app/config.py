"""
Central configuration, entirely env-driven so the container can be retargeted
(e.g. at a self-hosted Virtuoso instance instead of the public DBPedia
endpoint) without touching code.
"""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # SPARQL endpoint to hydrate the graph from. Point this at a self-hosted
    # Virtuoso instance later just by changing the env var.
    sparql_endpoint: str = os.getenv("SPARQL_ENDPOINT", "https://dbpedia.org/sparql")

    # DBPedia resource local names (e.g. "Category:Birds") are expanded to
    # full IRIs using this prefix when querying, and stripped back to local
    # names when parsing results — this is what the graph is keyed on.
    dbpedia_resource_prefix: str = "http://dbpedia.org/resource/"

    # Hard ceiling on max_depth regardless of what a caller requests, so a
    # single /dig call can't fan out into an unbounded live-SPARQL crawl.
    max_depth_hard_cap: int = int(os.getenv("MAX_DEPTH_HARD_CAP", "6"))

    # Total number of live SPARQL calls a single HTTP request is allowed to
    # spend across every seed in its batch (structure + synonym hydration
    # combined). Once exhausted, traversal/enrichment stops gracefully and
    # whatever was already hydrated (this call or a previous one) is returned.
    max_sparql_calls_per_request: int = int(os.getenv("MAX_SPARQL_CALLS_PER_REQUEST", "10000"))

    # Per-SPARQL-call HTTP read timeout. Public DBpedia often exceeds this on
    # reverse lookups (redirects/disambiguations); those are retried once then
    # skipped so /dig can continue.
    sparql_timeout_seconds: float = float(os.getenv("SPARQL_TIMEOUT_SECONDS", "15"))

    # Minimum gap (seconds) enforced between successive outgoing SPARQL calls,
    # across the whole process (all requests share one clock). DBpedia's
    # public endpoint documents a 100 req/s per-IP ceiling with a 120-request
    # burst allowance, but in practice (shared/NAT'd IPs, anonymous traffic)
    # 429s show up well below that. 0.5s (~2 req/s) is a conservative buffer
    # under the documented limit — tune via env var per environment.
    sparql_min_interval_seconds: float = float(os.getenv("SPARQL_MIN_INTERVAL_SECONDS", "0.5"))

    # On a 429/503 from the SPARQL endpoint, how many times to retry (with
    # backoff) before giving up on that one call and letting it fail.
    sparql_max_retries: int = int(os.getenv("SPARQL_MAX_RETRIES", "3"))

    # Where the hydrated graph is persisted as JSON, so a container restart
    # doesn't have to re-hydrate everything from scratch. Mount this path as
    # a volume in production.
    cache_file_path: str = os.getenv("CACHE_FILE_PATH", "data/wiki_data.json")

    port: int = int(os.getenv("PORT", "5007"))


settings = Settings()