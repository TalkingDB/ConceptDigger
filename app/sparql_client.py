"""
Async SPARQL client. Each method here is the live-query replacement for one
of the legacy bulk .nt dump files:

    skos_categories_en.nt          -> fetch_subcategories   (skos:broader)
    article_categories_en.nt       -> fetch_member_articles (dct:subject)
    labels_en.nt                   -> fetch_labels          (rdfs:label)
    mappingbased_properties_en.nt  -> fetch_alt_names       (dbo:alias, dbp:name,
                                                              dbp:alternateName, foaf:name)
    redirects_en.nt                -> fetch_redirect_sources    (dbo:wikiPageRedirects)
    disambiguations_en.nt          -> fetch_disambiguation_sources (dbo:wikiPageDisambiguates)

bold_keywords.nt has no SPARQL equivalent — it was mined from raw Wikipedia
XML dumps by wikipedia_bold_to_ntriple_file.py, which stays a standalone,
decoupled offline tool (see that file). It is not part of live hydration.

Note on direction for redirects/disambiguations: the legacy code read
"<subject> predicate <object>" and attached the *subject's* URI-derived label
as a synonym of the *object* (e.g. a redirect page's name becomes an
alternate name for the canonical page it points to). To reproduce that for
an entity E, we query for the *subjects* that point at E.
"""
import asyncio
import time
from typing import List

import httpx

from .config import settings

# HTTP statuses worth retrying after a backoff — 429 (rate limited) and 503
# (endpoint temporarily overloaded), matching DBpedia's own operational
# guidance for its public SPARQL endpoint.
_RETRYABLE_STATUSES = {429, 502, 503, 504}
# Timeouts get one extra attempt; 429/5xx keep SPARQL_MAX_RETRIES.
_TIMEOUT_RETRIES = 1
_RETRYABLE_EXCEPTIONS = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
)


class SparqlTransientError(Exception):
    """DBpedia/SPARQL was slow or unavailable after retries. Safe to skip this lookup."""

_PREFIXES = """
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
PREFIX dct: <http://purl.org/dc/terms/>
PREFIX dbo: <http://dbpedia.org/ontology/>
PREFIX dbp: <http://dbpedia.org/property/>
PREFIX foaf: <http://xmlns.com/foaf/0.1/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
"""


def _to_full_uri(local_name: str) -> str:
    return f"<{settings.dbpedia_resource_prefix}{local_name}>"


def _local_name_from_uri(uri: str) -> str:
    prefix = settings.dbpedia_resource_prefix
    return uri[len(prefix):] if uri.startswith(prefix) else uri


class SparqlClient:
    def __init__(
        self,
        endpoint: str,
        timeout: float,
        min_interval: float = 0.0,
        max_retries: int = 0,
    ):
        self.endpoint = endpoint
        self.timeout = timeout
        self.min_interval = min_interval
        self.max_retries = max_retries
        # Guards the shared "last call" clock below so concurrent requests
        # (different /dig batches, hydrate calls, etc.) all queue through the
        # same throttle instead of racing past it in parallel.
        self._throttle_lock = asyncio.Lock()
        self._last_call_at = 0.0

    async def _throttle(self) -> None:
        """Block until at least `min_interval` seconds have passed since the
        previous outgoing SPARQL call, across the whole process."""
        if self.min_interval <= 0:
            return
        async with self._throttle_lock:
            now = time.monotonic()
            wait = self.min_interval - (now - self._last_call_at)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call_at = time.monotonic()

    def _timeout(self) -> httpx.Timeout:
        read = float(self.timeout)
        return httpx.Timeout(read, connect=min(10.0, read), pool=10.0)

    async def _backoff(self, attempt: int, retry_after: str | None = None) -> None:
        try:
            delay = float(retry_after) if retry_after else 2.0 * attempt
        except ValueError:
            delay = 2.0 * attempt
        await asyncio.sleep(delay)

    async def _execute(self, client: httpx.AsyncClient, query: str) -> dict:
        """Shared retry/throttle loop. Returns the raw decoded JSON payload;
        callers pull out either `results.bindings` (SELECT) or `boolean`
        (ASK). Timeouts and 429/502/503/504 are retried, then raised as
        SparqlTransientError so /dig can skip this lookup instead of 500ing.
        """
        last_error: Exception | None = None
        attempt = 0
        while True:
            await self._throttle()
            try:
                resp = await client.post(
                    self.endpoint,
                    data={
                        "query": _PREFIXES + query,
                        "format": "application/sparql-results+json",
                    },
                    headers={"Accept": "application/sparql-results+json"},
                    timeout=self._timeout(),
                )
            except _RETRYABLE_EXCEPTIONS as exc:
                last_error = exc
                retry_limit = _TIMEOUT_RETRIES if isinstance(exc, httpx.TimeoutException) else self.max_retries
                if attempt >= retry_limit:
                    raise SparqlTransientError(
                        f"SPARQL lookup failed after {attempt + 1} attempt(s): {exc}"
                    ) from exc
                attempt += 1
                await self._backoff(attempt)
                continue

            if resp.status_code in _RETRYABLE_STATUSES and attempt < self.max_retries:
                last_error = httpx.HTTPStatusError(
                    f"SPARQL HTTP {resp.status_code}",
                    request=resp.request,
                    response=resp,
                )
                attempt += 1
                await self._backoff(attempt, resp.headers.get("Retry-After"))
                continue

            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code >= 500:
                    raise SparqlTransientError(
                        f"SPARQL endpoint returned HTTP {exc.response.status_code}"
                    ) from exc
                raise
            return resp.json()

    async def _run(self, client: httpx.AsyncClient, query: str) -> List[dict]:
        payload = await self._execute(client, query)
        return payload["results"]["bindings"]

    async def _run_ask(self, client: httpx.AsyncClient, query: str) -> bool:
        payload = await self._execute(client, query)
        return bool(payload.get("boolean", False))

    async def fetch_subcategories(self, client: httpx.AsyncClient, local_name: str) -> List[str]:
        q = f"SELECT DISTINCT ?child WHERE {{ ?child skos:broader {_to_full_uri(local_name)} }}"
        rows = await self._run(client, q)
        return [_local_name_from_uri(r["child"]["value"]) for r in rows]

    async def fetch_member_articles(self, client: httpx.AsyncClient, local_name: str) -> List[str]:
        q = f"SELECT DISTINCT ?article WHERE {{ ?article dct:subject {_to_full_uri(local_name)} }}"
        rows = await self._run(client, q)
        return [_local_name_from_uri(r["article"]["value"]) for r in rows]

    async def fetch_labels(self, client: httpx.AsyncClient, local_name: str) -> List[str]:
        uri = _to_full_uri(local_name)
        q = f"SELECT DISTINCT ?label WHERE {{ {uri} rdfs:label ?label . FILTER (lang(?label) = 'en') }}"
        rows = await self._run(client, q)
        return [r["label"]["value"] for r in rows]

    async def fetch_alt_names(self, client: httpx.AsyncClient, local_name: str) -> List[str]:
        uri = _to_full_uri(local_name)
        q = (
            "SELECT DISTINCT ?name WHERE { "
            f"{{ {uri} dbo:alias ?name }} UNION "
            f"{{ {uri} dbp:name ?name }} UNION "
            f"{{ {uri} dbp:alternateName ?name }} UNION "
            f"{{ {uri} foaf:name ?name }} "
            "}"
        )
        rows = await self._run(client, q)
        return [r["name"]["value"] for r in rows]

    async def fetch_redirect_sources(self, client: httpx.AsyncClient, local_name: str) -> List[str]:
        q = f"SELECT DISTINCT ?s WHERE {{ ?s dbo:wikiPageRedirects {_to_full_uri(local_name)} }}"
        rows = await self._run(client, q)
        return [_local_name_from_uri(r["s"]["value"]) for r in rows]

    async def fetch_disambiguation_sources(self, client: httpx.AsyncClient, local_name: str) -> List[str]:
        q = f"SELECT DISTINCT ?s WHERE {{ ?s dbo:wikiPageDisambiguates {_to_full_uri(local_name)} }}"
        rows = await self._run(client, q)
        return [_local_name_from_uri(r["s"]["value"]) for r in rows]

    async def ask_category_exists(self, client: httpx.AsyncClient, local_name: str) -> bool:
        """Single cheap ASK — true if the resource is a known category (either
        typed as skos:Concept, or has at least one skos:broader edge in
        either direction). Used to fail fast on a misspelled/unknown category
        before spending the 2 calls a full structure hydration would cost."""
        uri = _to_full_uri(local_name)
        q = (
            "ASK { "
            f"{{ {uri} a skos:Concept }} UNION "
            f"{{ {uri} skos:broader ?parent }} UNION "
            f"{{ ?child skos:broader {uri} }} "
            "}"
        )
        return await self._run_ask(client, q)


sparql_client = SparqlClient(
    settings.sparql_endpoint,
    settings.sparql_timeout_seconds,
    min_interval=settings.sparql_min_interval_seconds,
    max_retries=settings.sparql_max_retries,
)