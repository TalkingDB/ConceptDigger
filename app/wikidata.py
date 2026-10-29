"""
Experimental Wikidata digger behind POST /api/v1/wikidata/dig, built to be
compared against the DBpedia /dig. Deliberately kept in one flat module.

Wikidata has no Wikipedia-style category membership (its "Category:..." items
carry no member list), so the tree walked here is Wikidata's own class tree,
keyed by QID:

    class  <--P279 subclass of--  child   ("subcategory", kind=subclass)
    class  <--P31  instance of--  child   ("article",     kind=instance)

Every child is recursed into, not only subclasses, because Wikidata items are
often both (sugar is an instance of "food ingredient" and has subclasses like
brown sugar). Depth semantics match /dig: max_depth=0 returns the seed's
direct children.

Synonym sources, batched 50 entities per call where the API allows it:
    wikidata:label         labels in WIKIDATA_LANGUAGES         (wbgetentities)
    wikidata:alias         aliases in WIKIDATA_LANGUAGES        (wbgetentities)
    enwiki:title           English Wikipedia article title      (wbgetentities)
    wikidata:Pxxx          name-like statements, e.g. P1843 taxon common name (SPARQL)
    enwiki:redirect        redirects to the article             (Wikipedia API)
    enwiki:disambiguation  disambiguation pages linking to it   (Wikipedia search, opt-in,
                           one call per entity)

All of it lives in its own GraphStore (data/wikidata_cache.json), so it never
touches the DBpedia graph_cache.json.
"""
import asyncio
import logging
import re
import time
from typing import Dict, Iterable, List, Literal, Optional, Sequence, Tuple
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field

from .config import settings
from .dig import SurfaceForm
from .graph_store import GraphStore
from .hydration import SparqlBudget
from .utils import convert_entity_uri_to_label

logger = logging.getLogger(__name__)

ENTITY_PREFIX = "http://www.wikidata.org/entity/"
STRUCTURE_BATCH = 25
SYNONYM_BATCH = 50
REQUEST_TIMEOUT = httpx.Timeout(65.0, connect=10.0)  # query.wikidata.org stops at 60s
RETRY_STATUSES = {429, 500, 502, 503, 504}

# Name-like statements worth treating as surface forms.
NAME_PROPERTIES = {
    "P1813": "short name",
    "P1449": "nickname",
    "P1448": "official name",
    "P1843": "taxon common name",
    "P225": "taxon name",
    "P2561": "name",
    "P4970": "alternative name",
    "P1705": "native label",
    "P742": "pseudonym",
}

_QID_RE = re.compile(r"(?:^|[/:=])(Q\d+)$", re.IGNORECASE)


def parse_qid(value: str) -> Optional[str]:
    """Accept Q42, wd:Q42 or a wikidata.org /wiki/ or /entity/ URL."""
    raw = (value or "").strip().strip("<>").rstrip("/")
    match = _QID_RE.search(raw)
    return match.group(1).upper() if match else None


def _qid_from_uri(uri: str) -> Optional[str]:
    if not uri.startswith(ENTITY_PREFIX + "Q"):
        return None
    return uri[len(ENTITY_PREFIX):]


def _chunks(items: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _languages() -> List[str]:
    return [lang for lang in settings.wikidata_languages.split("|") if lang]


class HostThrottle:
    """Process-wide spacing of calls to one host, like SparqlClient._throttle
    for DBpedia. A 429 pushes the next slot back for every caller, not only
    the one that got it, so the parallel tasks stop hammering together."""

    def __init__(self, interval: float):
        self.interval = interval
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def wait(self) -> None:
        async with self._lock:
            delay = self._next_at - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_at = time.monotonic() + self.interval

    def back_off(self, seconds: float) -> None:
        self._next_at = max(self._next_at, time.monotonic() + seconds)


class WikimediaClient:
    """GETs JSON from Wikidata/Wikipedia with retries. Every call is counted
    on the budget; a call that still fails after retries is recorded as a
    failure and returns None so the dig can carry on."""

    def __init__(self, http_client: httpx.AsyncClient):
        self.http = http_client
        self._gate = asyncio.Semaphore(settings.wikidata_concurrency)
        self._headers = {"User-Agent": settings.wikimedia_user_agent, "Accept": "application/json"}
        wikipedia_host = urlparse(settings.wikipedia_api_url).netloc
        self._throttles = {
            host: HostThrottle(
                settings.wikipedia_min_interval_seconds if host == wikipedia_host
                else settings.wikidata_min_interval_seconds
            )
            for host in {
                wikipedia_host,
                urlparse(settings.wikidata_api_url).netloc,
                urlparse(settings.wikidata_sparql_endpoint).netloc,
            }
        }

    async def get_json(self, url: str, params: Dict[str, str], budget: SparqlBudget) -> Optional[dict]:
        budget.spend(1)
        throttle = self._throttles[urlparse(url).netloc]
        error = ""
        for attempt in range(settings.sparql_max_retries + 1):
            delay = 2.0 ** attempt
            await throttle.wait()
            try:
                async with self._gate:
                    resp = await self.http.get(url, params=params, headers=self._headers, timeout=REQUEST_TIMEOUT)
            except httpx.HTTPError as exc:
                error = repr(exc)
            else:
                if resp.status_code == 200:
                    data = resp.json()
                    # MediaWiki reports some errors (maxlag, bad ids) with a 200.
                    if isinstance(data, dict) and "error" in data:
                        error = f"API error {data['error'].get('code')}: {data['error'].get('info')}"
                        if data["error"].get("code") != "maxlag":
                            break
                    else:
                        return data
                else:
                    error = f"HTTP {resp.status_code}"
                    if resp.status_code not in RETRY_STATUSES:
                        break
                    retry_after = resp.headers.get("Retry-After", "")
                    if retry_after.isdigit():
                        delay = float(retry_after)
            if attempt < settings.sparql_max_retries:
                throttle.back_off(delay)
        budget.record_failure(1)
        kind = params.get("list") or params.get("prop") or params.get("action") or "sparql"
        logger.warning("wikimedia request failed: url=%s kind=%s err=%s", url, kind, error)
        return None

    async def sparql(self, query: str, budget: SparqlBudget) -> Optional[List[dict]]:
        data = await self.get_json(settings.wikidata_sparql_endpoint, {"query": query, "format": "json"}, budget)
        return None if data is None else data["results"]["bindings"]


class WikidataService:
    def __init__(self, store: GraphStore, http_client: httpx.AsyncClient):
        self.store = store
        self.client = WikimediaClient(http_client)

    def _ensure_node(self, qid: str) -> None:
        # Not store.ensure_node: that seeds a synonym from the key, which for
        # a QID would just be "q123".
        if not self.store.has_node(qid):
            self.store.graph.add_node(
                qid, synonyms=[], kinds=[], structure_hydrated=False, synonyms_hydrated=False,
            )

    async def persist(self) -> None:
        async with self.store.write_lock():
            await self.store.persist()

    # ------------------------------------------------------------------
    # seed lookup / search
    # ------------------------------------------------------------------
    async def entity_exists(self, qid: str, budget: SparqlBudget) -> Optional[bool]:
        if self.store.is_structure_hydrated(qid):
            return True
        params = {"query": f"ASK {{ wd:{qid} ?p ?o }}", "format": "json"}
        data = await self.client.get_json(settings.wikidata_sparql_endpoint, params, budget)
        return None if data is None else bool(data.get("boolean"))

    async def search(self, text: str, limit: int) -> List[dict]:
        params = {
            "action": "wbsearchentities", "search": text, "language": "en",
            "limit": str(limit), "format": "json",
        }
        data = await self.client.get_json(settings.wikidata_api_url, params, SparqlBudget())
        return [
            {"id": r["id"], "label": r.get("label"), "description": r.get("description")}
            for r in (data or {}).get("search", [])
        ]

    # ------------------------------------------------------------------
    # structure: P279 subclasses + P31 instances
    # ------------------------------------------------------------------
    def _children_query(self, parents: Sequence[str], limit: int) -> str:
        values = " ".join(f"wd:{q}" for q in parents)
        return f"""
SELECT ?parent ?child ?kind WHERE {{
  VALUES ?parent {{ {values} }}
  {{ ?child wdt:P279 ?parent . BIND("subclass" AS ?kind) }}
  UNION
  {{ ?child wdt:P31 ?parent . BIND("instance" AS ?kind) }}
}} LIMIT {limit}"""

    async def _hydrate_children(self, parents: Sequence[str], budget: SparqlBudget) -> None:
        cap = settings.wikidata_max_children_per_node
        rows = await self.client.sparql(self._children_query(parents, cap * len(parents)), budget)
        if rows is None and len(parents) > 1:
            # Usually one oversized class timing the batch out. Retry each
            # parent alone, where the per-node LIMIT keeps it fast.
            logger.info("wikidata structure batch failed, retrying %d nodes one by one", len(parents))
            await asyncio.gather(*(self._hydrate_children([p], budget) for p in parents))
            return
        if rows is None:
            return
        if len(rows) >= cap * len(parents):
            logger.warning("wikidata children truncated at %d rows for %s", len(rows), ",".join(parents))

        async with self.store.write_lock():
            for row in rows:
                parent = _qid_from_uri(row["parent"]["value"])
                child = _qid_from_uri(row["child"]["value"])
                if parent is None or child is None or child == parent:
                    continue
                self._ensure_node(child)
                kinds = self.store.graph.nodes[child].setdefault("kinds", [])
                if row["kind"]["value"] not in kinds:
                    kinds.append(row["kind"]["value"])
                self.store.ensure_edge(parent, child)
            for parent in parents:
                self._ensure_node(parent)
                self.store.mark_structure_hydrated(parent)

    async def walk(self, seed: str, max_depth: int, budget: SparqlBudget) -> List[str]:
        """Breadth-first walk; returns every descendant QID once, in BFS order."""
        seen = {seed}
        frontier = [seed]
        collected: List[str] = []
        for depth in range(max_depth + 1):
            todo = [q for q in frontier if not self.store.is_structure_hydrated(q)]
            await asyncio.gather(*(
                self._hydrate_children(batch, budget) for batch in _chunks(todo, STRUCTURE_BATCH)
            ))
            next_frontier = []
            for qid in frontier:
                for child in self.store.children(qid):
                    if child not in seen:
                        seen.add(child)
                        next_frontier.append(child)
            collected += next_frontier
            logger.info(
                "wikidata dig progress: seed=%s depth=%s/%s expanded=%s new=%s collected=%s calls=%s",
                seed, depth, max_depth, len(frontier), len(next_frontier), len(collected), budget.used,
            )
            await self.persist()
            frontier = next_frontier
            if not frontier:
                break
        return collected

    # ------------------------------------------------------------------
    # synonyms
    # ------------------------------------------------------------------
    async def _fetch_entities(self, qids: Sequence[str], budget: SparqlBudget) -> Optional[Dict[str, dict]]:
        params = {
            "action": "wbgetentities", "ids": "|".join(qids),
            "props": "labels|aliases|sitelinks", "languages": "|".join(_languages()),
            "sitefilter": "enwiki", "format": "json",
        }
        data = await self.client.get_json(settings.wikidata_api_url, params, budget)
        if data is None:
            return None
        entities = {}
        for key, entity in data.get("entities", {}).items():
            # A merged item comes back under its new id; file it under ours.
            entities[entity.get("redirects", {}).get("from", key)] = entity
        return entities

    async def _fetch_name_statements(
        self, qids: Sequence[str], budget: SparqlBudget,
    ) -> Optional[Dict[str, List[Tuple[str, str]]]]:
        langs = ", ".join(f'"{lang}"' for lang in ["", *_languages()])
        query = f"""
SELECT ?item ?prop ?value WHERE {{
  VALUES ?item {{ {" ".join(f"wd:{q}" for q in qids)} }}
  VALUES ?prop {{ {" ".join(f"wdt:{p}" for p in NAME_PROPERTIES)} }}
  ?item ?prop ?value .
  FILTER(isLiteral(?value) && lang(?value) IN ({langs}))
}}"""
        rows = await self.client.sparql(query, budget)
        if rows is None:
            return None
        names: Dict[str, List[Tuple[str, str]]] = {}
        for row in rows:
            qid = _qid_from_uri(row["item"]["value"])
            prop = row["prop"]["value"].rsplit("/", 1)[-1]
            if qid:
                names.setdefault(qid, []).append((row["value"]["value"], f"wikidata:{prop}"))
        return names

    async def _fetch_redirects(self, titles: Sequence[str], budget: SparqlBudget) -> Optional[Dict[str, List[str]]]:
        redirects: Dict[str, List[str]] = {title: [] for title in titles}
        if not titles:
            return redirects
        params = {
            "action": "query", "prop": "redirects", "titles": "|".join(titles),
            "rdprop": "title", "rdnamespace": "0", "rdlimit": "max",
            "format": "json", "formatversion": "2",
        }
        cont: Dict[str, str] = {}
        while True:
            data = await self.client.get_json(settings.wikipedia_api_url, {**params, **cont}, budget)
            if data is None:
                return None
            query = data.get("query", {})
            normalized = {n["to"]: n["from"] for n in query.get("normalized", [])}
            for page in query.get("pages", []):
                key = normalized.get(page["title"], page["title"])
                redirects.setdefault(key, []).extend(r["title"] for r in page.get("redirects", []))
            if "continue" not in data:
                return redirects
            cont = data["continue"]

    async def _hydrate_synonym_batch(self, qids: Sequence[str], budget: SparqlBudget) -> None:
        entities, names = await asyncio.gather(
            self._fetch_entities(qids, budget), self._fetch_name_statements(qids, budget),
        )
        titles = {
            qid: entity["sitelinks"]["enwiki"]["title"]
            for qid, entity in (entities or {}).items()
            if "enwiki" in entity.get("sitelinks", {})
        }
        redirects = await self._fetch_redirects(list(titles.values()), budget)
        complete = entities is not None and names is not None and redirects is not None

        async with self.store.write_lock():
            for qid in qids:
                entity = (entities or {}).get(qid, {})
                collected: List[Tuple[str, str]] = []
                for lang in _languages():
                    label = entity.get("labels", {}).get(lang)
                    if label:
                        collected.append((label["value"], "wikidata:label"))
                for lang in _languages():
                    collected += [(a["value"], "wikidata:alias") for a in entity.get("aliases", {}).get(lang, [])]
                title = titles.get(qid)
                if title:
                    collected.append((convert_entity_uri_to_label(title), "enwiki:title"))
                collected += (names or {}).get(qid, [])
                if title:
                    collected += [
                        (convert_entity_uri_to_label(r), "enwiki:redirect")
                        for r in (redirects or {}).get(title, [])
                    ]

                self._ensure_node(qid)
                node = self.store.graph.nodes[qid]
                en_label = entity.get("labels", {}).get("en")
                if en_label:
                    node["label"] = en_label["value"]
                if title:
                    node["enwiki_title"] = title
                self.store.add_synonyms(qid, [(text.strip().lower(), src) for text, src in collected])
                if complete:
                    self.store.mark_synonyms_hydrated(qid)

    async def _hydrate_disambiguation(self, qid: str, title: str, budget: SparqlBudget) -> None:
        params = {
            "action": "query", "list": "search", "srnamespace": "0", "srlimit": "50", "srprop": "",
            "srsearch": f'linksto:"{title.replace(chr(34), "")}" incategory:"All_disambiguation_pages"',
            "format": "json", "formatversion": "2",
        }
        data = await self.client.get_json(settings.wikipedia_api_url, params, budget)
        if data is None:
            return
        pages = [hit["title"] for hit in data.get("query", {}).get("search", [])]
        async with self.store.write_lock():
            self.store.add_synonyms(
                qid, [(convert_entity_uri_to_label(p), "enwiki:disambiguation") for p in pages],
            )
            self.store.graph.nodes[qid]["disambiguation_hydrated"] = True

    async def hydrate_synonyms(self, qids: Sequence[str], budget: SparqlBudget, include_disambiguation: bool) -> None:
        todo = [q for q in qids if not self.store.is_synonyms_hydrated(q)]
        await asyncio.gather(*(self._hydrate_synonym_batch(batch, budget) for batch in _chunks(todo, SYNONYM_BATCH)))
        logger.info("wikidata synonyms hydrated: entities=%d calls=%d", len(todo), budget.used)

        if include_disambiguation:
            nodes = self.store.graph.nodes
            todo = [
                q for q in qids
                if nodes[q].get("enwiki_title") and not nodes[q].get("disambiguation_hydrated")
            ]
            await asyncio.gather(*(self._hydrate_disambiguation(q, nodes[q]["enwiki_title"], budget) for q in todo))
            logger.info("wikidata disambiguations hydrated: entities=%d calls=%d", len(todo), budget.used)

        await self.persist()


# ----------------------------------------------------------------------
# request/response + orchestration
# ----------------------------------------------------------------------
class WikidataDigResultItem(BaseModel):
    entity_url: str
    entity: str
    label: Optional[str] = None
    wikipedia_title: Optional[str] = None
    kinds: List[str] = Field(default_factory=list, description="subclass and/or instance")
    surface_text: List[SurfaceForm]
    seed_category: str


class WikidataDigResponse(BaseModel):
    data: List[WikidataDigResultItem] = Field(default_factory=list)
    status: Literal["complete", "incomplete", "failed"]
    requests_used: int = Field(ge=0, description="Wikidata/Wikipedia calls made by this request.")
    failed_requests: int = Field(ge=0)
    error: Optional[str] = None


async def run_wikidata_dig(
    service: WikidataService,
    seeds: List[str],
    max_depth: int,
    return_categories: bool,
    return_pages: bool,
    only_with_wikipedia_article: bool,
    include_disambiguation: bool,
    budget: SparqlBudget,
) -> WikidataDigResponse:
    store = service.store
    matched: Dict[Tuple[str, str], None] = {}

    try:
        for seed in seeds:
            logger.info("wikidata dig started: seed=%s max_depth=%s", seed, max_depth)
            for qid in await service.walk(seed, max_depth, budget):
                kinds = store.graph.nodes[qid].get("kinds", [])
                if (return_categories and "subclass" in kinds) or (return_pages and "instance" in kinds):
                    matched[(qid, seed)] = None

        await service.hydrate_synonyms(list({qid for qid, _ in matched}), budget, include_disambiguation)
    except asyncio.CancelledError:
        # Client went away. Keep what was already fetched so the next dig
        # resumes from it instead of re-fetching.
        logger.info("wikidata dig cancelled after %d calls, saving progress", budget.used)
        await service.persist()
        raise

    data = []
    for qid, seed in matched:
        node = store.graph.nodes[qid]
        if only_with_wikipedia_article and not node.get("enwiki_title"):
            continue
        surface_text = [{"text": text, "how_this_record": source} for text, source in store.synonyms(qid)]
        if not surface_text:
            continue
        data.append(WikidataDigResultItem(
            entity_url=f"Wikidata>{qid}",
            entity=qid,
            label=node.get("label"),
            wikipedia_title=node.get("enwiki_title"),
            kinds=node.get("kinds", []),
            surface_text=surface_text,
            seed_category=seed,
        ))

    logger.info(
        "wikidata dig finished: seeds=%d matched=%d returned=%d calls=%d failed=%d",
        len(seeds), len(matched), len(data), budget.used, budget.failed,
    )

    if budget.used > 0 and budget.failed == budget.used and not data:
        status, error = "failed", f"All {budget.failed} Wikimedia requests failed."
    elif budget.failed:
        status, error = "incomplete", f"{budget.failed} Wikimedia requests failed. Results may be incomplete."
    else:
        status, error = "complete", None
    return WikidataDigResponse(
        data=data, status=status, requests_used=budget.used, failed_requests=budget.failed, error=error,
    )
