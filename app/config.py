"""
Central configuration, entirely env-driven so the container can be retargeted
(e.g. at a self-hosted Virtuoso instance instead of the public DBPedia
endpoint) without touching code.
"""
import os
from dataclasses import dataclass, field


def _env_str(name: str, default: str) -> str:
    """Read an env var; blank or missing values fall back to default.

    `.env.example` ships keys as empty (`EXPORT_MAX_FILES=`), and DevPod copies
    that file to `.env`, so `int(os.getenv(...))` would crash on startup.
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip()


def _env_int(name: str, default: int) -> int:
    return int(_env_str(name, str(default)))


def _env_float(name: str, default: float) -> float:
    return float(_env_str(name, str(default)))


@dataclass(frozen=True)
class Settings:
    # SPARQL endpoint to hydrate the graph from. Point this at a self-hosted
    # Virtuoso instance later just by changing the env var.
    sparql_endpoint: str = field(default_factory=lambda: _env_str("SPARQL_ENDPOINT", "https://dbpedia.org/sparql"))

    # DBPedia resource local names (e.g. "Category:Birds") are expanded to
    # full IRIs using this prefix when querying, and stripped back to local
    # names when parsing results — this is what the graph is keyed on.
    dbpedia_resource_prefix: str = "http://dbpedia.org/resource/"

    # Hard ceiling on max_depth regardless of what a caller requests, so a
    # single /dig call can't fan out into an unbounded live-SPARQL crawl.
    max_depth_hard_cap: int = field(default_factory=lambda: _env_int("MAX_DEPTH_HARD_CAP", 6))

    # Per-SPARQL-call HTTP read timeout. Public DBpedia often exceeds this on
    # reverse lookups (redirects/disambiguations); those are retried once then
    # skipped so /dig can continue.
    sparql_timeout_seconds: float = field(default_factory=lambda: _env_float("SPARQL_TIMEOUT_SECONDS", 20))

    # Minimum gap (seconds) enforced between successive outgoing SPARQL calls,
    # across the whole process (all requests share one clock). DBpedia's
    # public endpoint documents a 100 req/s per-IP ceiling with a 120-request
    # burst allowance, but in practice (shared/NAT'd IPs, anonymous traffic)
    # 429s show up well below that. 0.5s (~2 req/s) is a conservative buffer
    # under the documented limit — tune via env var per environment.
    sparql_min_interval_seconds: float = field(default_factory=lambda: _env_float("SPARQL_MIN_INTERVAL_SECONDS", 0.5))

    # On a 429/503 from the SPARQL endpoint, how many times to retry (with
    # backoff) before giving up on that one call and letting it fail.
    sparql_max_retries: int = field(default_factory=lambda: _env_int("SPARQL_MAX_RETRIES", 3))

    # Where the hydrated graph is persisted as JSON, so a container restart
    # doesn't have to re-hydrate everything from scratch. Mount this path as
    # a volume in production.
    cache_file_path: str = field(default_factory=lambda: _env_str("CACHE_FILE_PATH", "data/graph_cache.json"))

    # After each successful export write, older JSON files in data/exports/
    # are deleted until at most this many remain. 0 disables pruning.
    export_max_files: int = field(default_factory=lambda: _env_int("EXPORT_MAX_FILES", 50))

    port: int = field(default_factory=lambda: _env_int("PORT", 5007))


settings = Settings()
