import copy
import httpx
from httpx import ASGITransport
from starlette.datastructures import State

from app.graph_store import GraphStore
from app.hydration import HydrationService
from app.main import app
from app.sparql_client import SparqlTransientError


class Stub:
    """Simulate a category lookup with configurable SPARQL failures and return values."""
    def __init__(
        self,
        fail=(),
        fail_categories=(),
        articles=("Sodium_chloride",),
        subcategories=(),
        labels=("Sodium chloride",),
        alt_names=(),
        redirects=("NaCl",),
        disambiguations=(),
        ask_exists=True,
    ):
        self.fail = set(fail)
        self.fail_categories = set(fail_categories)
        self.articles = list(articles)
        self.subcategories = list(subcategories)
        self.labels = list(labels)
        self.alt_names = list(alt_names)
        self.redirects = list(redirects)
        self.disambiguations = list(disambiguations)
        self.ask_exists = ask_exists
        self.calls = []

    def _should_fail(self, name, category):
        if name in self.fail:
            return True
        if category:
            cat_str = str(category)
            for fc in self.fail_categories:
                if fc in cat_str or cat_str.endswith(fc):
                    return True
        return False

    async def _go(self, name, value, category=None):
        self.calls.append((name, category))
        if self._should_fail(name, category):
            raise SparqlTransientError("SPARQL endpoint returned HTTP 503")
        return value

    async def ask_category_exists(self, c, n): return await self._go("ask", self.ask_exists, category=c)
    async def fetch_subcategories(self, c, n): return await self._go("subcategories", self.subcategories, category=c)
    async def fetch_member_articles(self, c, n): return await self._go("articles", self.articles, category=c)
    async def fetch_labels(self, c, n): return await self._go("labels", self.labels, category=c)
    async def fetch_alt_names(self, c, n): return await self._go("alt_names", self.alt_names, category=c)
    async def fetch_redirect_sources(self, c, n): return await self._go("redirects", self.redirects, category=c)
    async def fetch_disambiguation_sources(self, c, n): return await self._go("disambiguations", self.disambiguations, category=c)

async def dig(tmp_path, stub, categories=None, store=None):
    if store is None:
        store = GraphStore(str(tmp_path / "g.json"))
    if categories is None:
        categories = ["Category:Deliquescent_materials"]

    http = httpx.AsyncClient()
    test_app = copy.copy(app)
    test_app.state = State()
    test_app.state.store = store
    test_app.state.http_client = http
    test_app.state.hydration = HydrationService(store, stub, http)

    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=test_app),
            base_url="http://t",
        ) as client:
            return await client.post(
                "/api/v1/dig",
                json={
                    "categories": categories,
                    "max_depth": 0,
                },
            )
    finally:
        await http.aclose()
