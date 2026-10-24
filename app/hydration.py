"""
HydrationService is the only thing allowed to call out to SPARQL and write
new nodes/edges/synonyms into the GraphStore. Two mechanisms live here:

  - SparqlBudget: a small mutable counter shared across every seed in one
    HTTP request. Every live SPARQL call adds 1, so the request can log how
    many calls it made. There is no upper limit.

  - De-duped hydration (via GraphStore.run_deduped): if two requests arrive
    at the same moment both wanting to hydrate "Category:Birds", only one
    actually fires the SPARQL calls; the other awaits that same result.
"""
import logging
from typing import List, Tuple

import httpx

from .graph_store import GraphStore
from .sparql_client import SparqlClient, SparqlTransientError
from .utils import convert_entity_uri_to_label

logger = logging.getLogger(__name__)

SOURCE_LABEL = "rdfs:label"
SOURCE_ALT_NAME = "alt_name"
SOURCE_DISAMBIGUATION = "disambiguation"
SOURCE_REDIRECT = "redirect"


class SparqlBudget:
    """Counts live SPARQL calls per request."""

    def __init__(self):
        self.used = 0

    def spend(self, n: int = 1) -> None:
        self.used += n

    def record_failure(self, n: int = 1) -> None:
        self.failed += n

    @property
    def exhausted(self) -> bool:
        return self.used >= self.limit


class HydrationService:
    def __init__(self, store: GraphStore, sparql_client: SparqlClient, http_client: httpx.AsyncClient):
        self.store = store
        self.sparql_client = sparql_client
        self.http_client = http_client

    async def _lookup( self, kind: str, local_name: str, fetch, budget: SparqlBudget) -> Tuple[list | None, bool]:
        """Run one SPARQL fetch and record transient failures."""
        try:
            return await fetch(self.http_client, local_name), True
        except SparqlTransientError as exc:
            budget.record_failure(1)
            logger.warning(
                "SPARQL %s failed: node=%s err=%s — skipping this lookup and continuing /dig",
                kind, local_name, exc,
            )
            return None, False
        finally:
            budget.spend(1)

    # ------------------------------------------------------------------
    # structure: skos:broader (subcategories) + dct:subject (member articles)
    # ------------------------------------------------------------------
    async def hydrate_category_structure(self, local_name: str, budget: SparqlBudget) -> Tuple[int, int]:
        key = f"structure:{local_name}"
        return await self.store.run_deduped(key, self._do_hydrate_category, local_name, budget)

    async def _do_hydrate_category(self, local_name: str, budget: SparqlBudget) -> Tuple[int, int]:
        if self.store.is_structure_hydrated(local_name):
            return (0, 0)

        structure_complete = True
        subcats, ok = await self._lookup("subcategories", local_name, self.sparql_client.fetch_subcategories, budget=budget)
        if not ok:
            return (0, 0)

        articles: List[str] = []
        fetched, ok = await self._lookup(
            "member articles", local_name, self.sparql_client.fetch_member_articles, budget=budget
        )
        if ok:
            articles = fetched or []
            budget.spend(1)
        else:
            structure_complete = False

        async with self.store.write_lock():
            new_nodes = 0
            new_edges = 0
            if self.store.ensure_node(local_name, SOURCE_LABEL):
                new_nodes += 1
            for child in (subcats or []):
                if self.store.ensure_node(child, SOURCE_LABEL):
                    new_nodes += 1
                if self.store.ensure_edge(local_name, child):
                    new_edges += 1
            for article in articles:
                if self.store.ensure_node(article, SOURCE_LABEL):
                    new_nodes += 1
                if self.store.ensure_edge(local_name, article):
                    new_edges += 1
            if structure_complete:
                self.store.mark_structure_hydrated(local_name)
            await self.store.persist()

        return (new_nodes, new_edges)

    async def category_exists(self, local_name: str, budget: SparqlBudget) -> bool | None:
        # Already in the graph with structure hydrated means we've proven
        # this one before — no need to ask DBpedia again.
        if self.store.is_structure_hydrated(local_name):
            return True
        key = f"exists:{local_name}"
        return await self.store.run_deduped(key, self._do_check_category_exists, local_name, budget)

    async def _do_check_category_exists(self, local_name: str, budget: SparqlBudget) -> bool | None:
        if self.store.is_structure_hydrated(local_name):
            return True
        
        if not budget.has_room():
            logger.warning(
                "budget exhausted before existence check could run: node=%s used=%d limit=%d "
                "— proceeding as if it exists rather than dropping it",
                local_name, budget.used, budget.limit,
            )
            return True

        try:
            budget.spend(1)
            return await self.sparql_client.ask_category_exists(
                self.http_client,
                local_name,
            )
        except SparqlTransientError as exc:
            budget.record_failure(1)
            logger.warning(
                "SPARQL category-existence check failed: node=%s err=%s",
                local_name,
                exc,
            )
            return None

    # ------------------------------------------------------------------
    # synonyms: rdfs:label, alt names, redirects, disambiguations
    # ------------------------------------------------------------------
    async def hydrate_synonyms(self, local_name: str, budget: SparqlBudget) -> int:
        key = f"synonyms:{local_name}"
        return await self.store.run_deduped(key, self._do_hydrate_synonyms, local_name, budget)

    async def _do_hydrate_synonyms(self, local_name: str, budget: SparqlBudget) -> int:
        if self.store.is_synonyms_hydrated(local_name):
            return 0

        collected: List[Tuple[str, str]] = []
        # Only mark the node hydrated once every one of the four lookups
        # below has completed. If one fails transiently, the node stays
        # unmarked so a later /dig retries it instead of leaving it
        # under-enriched forever.
        all_lookups_completed = True

        if budget.has_room():
            labels, ok = await self._lookup("labels", local_name, self.sparql_client.fetch_labels, budget=budget)
            if ok:
                collected += [(label.lower(), SOURCE_LABEL) for label in (labels or [])]
            else:
                all_lookups_completed = False
        else:
            all_lookups_completed = False
            logger.warning(
                "budget ran out before labels lookup: node=%s (used=%d limit=%d)",
                local_name, budget.used, budget.limit,
            )

        if budget.has_room():
            alt_names, ok = await self._lookup("alt-names", local_name, self.sparql_client.fetch_alt_names, budget=budget)
            if ok:
                collected += [(name.lower(), SOURCE_ALT_NAME) for name in (alt_names or [])]
            else:
                all_lookups_completed = False
        else:
            all_lookups_completed = False
            logger.warning(
                "budget ran out before alt-names lookup: node=%s (used=%d limit=%d)",
                local_name, budget.used, budget.limit,
            )

        if budget.has_room():
            redirect_sources, ok = await self._lookup(
                "redirects", local_name, self.sparql_client.fetch_redirect_sources, budget=budget
            )
            if ok:
                collected += [
                    (convert_entity_uri_to_label(s), SOURCE_REDIRECT) for s in (redirect_sources or [])
                ]
            else:
                all_lookups_completed = False
        else:
            all_lookups_completed = False
            logger.warning(
                "budget ran out before redirects lookup: node=%s (used=%d limit=%d)",
                local_name, budget.used, budget.limit,
            )

        if budget.has_room():
            disambig_sources, ok = await self._lookup(
                "disambiguations", local_name, self.sparql_client.fetch_disambiguation_sources, budget=budget
            )
            if ok:
                collected += [
                    (convert_entity_uri_to_label(s), SOURCE_DISAMBIGUATION)
                    for s in (disambig_sources or [])
                ]
            else:
                all_lookups_completed = False
        else:
            all_lookups_completed = False
            logger.warning(
                "budget ran out before disambiguations lookup: node=%s (used=%d limit=%d)",
                local_name, budget.used, budget.limit,
            )

        async with self.store.write_lock():
            self.store.ensure_node(local_name, SOURCE_LABEL)
            added = self.store.add_synonyms(local_name, collected)
            if all_lookups_completed:
                self.store.mark_synonyms_hydrated(local_name)
            else:
                logger.info(
                    "node left partially hydrated, will retry on a future /dig: node=%s",
                    local_name,
                )
            await self.store.persist()

        return added