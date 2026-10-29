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

    # Experimental Wikidata digger (/api/v1/wikidata/dig). Kept in its own
    # cache file so it never mixes with the DBpedia graph and the two can be
    # compared side by side.
    wikidata_cache_file_path: str = field(default_factory=lambda: _env_str("WIKIDATA_CACHE_FILE_PATH", "data/wikidata_cache.json"))
    wikidata_sparql_endpoint: str = field(default_factory=lambda: _env_str("WIKIDATA_SPARQL_ENDPOINT", "https://query.wikidata.org/sparql"))
    wikidata_api_url: str = field(default_factory=lambda: _env_str("WIKIDATA_API_URL", "https://www.wikidata.org/w/api.php"))
    wikipedia_api_url: str = field(default_factory=lambda: _env_str("WIKIPEDIA_API_URL", "https://en.wikipedia.org/w/api.php"))
    # Wikimedia answers 403 to programmatic clients whose user agent carries
    # no contact (URL or email): https://w.wiki/4wJS
    wikimedia_user_agent: str = field(default_factory=lambda: _env_str("WIKIMEDIA_USER_AGENT", "ConceptDigger/2.0 (https://github.com/TalkingDB/ConceptDigger)"))
    # Label/alias languages pulled from Wikidata, "|"-separated.
    wikidata_languages: str = field(default_factory=lambda: _env_str("WIKIDATA_LANGUAGES", "en|en-gb|en-us|en-ca|simple"))
    # Per-class cap on subclasses + instances, so a huge class (e.g. "human")
    # can't blow up a dig. Truncation is logged.
    wikidata_max_children_per_node: int = field(default_factory=lambda: _env_int("WIKIDATA_MAX_CHILDREN_PER_NODE", 5000))
    # Parallel outgoing Wikimedia requests. query.wikidata.org allows 5 per IP.
    wikidata_concurrency: int = field(default_factory=lambda: _env_int("WIKIDATA_CONCURRENCY", 4))
    # Minimum gap between calls to each host, process-wide (same idea as
    # SPARQL_MIN_INTERVAL_SECONDS). en.wikipedia.org rate-limits harder than
    # Wikidata, mostly the search API used for disambiguations.
    wikipedia_min_interval_seconds: float = field(default_factory=lambda: _env_float("WIKIPEDIA_MIN_INTERVAL_SECONDS", 0.2))
    wikidata_min_interval_seconds: float = field(default_factory=lambda: _env_float("WIKIDATA_MIN_INTERVAL_SECONDS", 0.1))

    # After each successful export write, older JSON files in data/exports/
    # are deleted until at most this many remain. 0 disables pruning.
    export_max_files: int = field(default_factory=lambda: _env_int("EXPORT_MAX_FILES", 50))

    elasticsearch_url: str = field(default_factory=lambda: _env_str("ELASTICSEARCH_URL", "http://localhost:9200"))
    elasticsearch_index: str = field(default_factory=lambda: _env_str("ELASTICSEARCH_INDEX", "train-synonyms"))

    port: int = field(default_factory=lambda: _env_int("PORT", 5007))


settings = Settings()
