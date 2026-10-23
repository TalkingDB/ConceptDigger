
import copy

import httpx
from httpx import ASGITransport
from starlette.datastructures import State

from app.graph_store import GraphStore
from app.hydration import HydrationService
from app.main import app
from app.sparql_client import SparqlTransientError


class Stub:
    """Simulate a category lookup with configurable SPARQL failures."""

    def __init__(self, fail=(), articles=("Sodium_chloride",)):
        self.fail = set(fail)
        self.articles = list(articles)
        self.calls = []

    async def _go(self, name, value):
        self.calls.append(name)
        if name in self.fail:
            raise SparqlTransientError(
                "SPARQL endpoint returned HTTP 503"
            )
        return value

    async def ask_category_exists(self, c, n):return await self._go("ask", True)
    async def fetch_subcategories(self, c, n):return await self._go("subcategories", [])
    async def fetch_member_articles(self, c, n):return await self._go("articles", self.articles)
    async def fetch_labels(self, c, n):return await self._go("labels", ["Sodium chloride"])
    async def fetch_alt_names(self, c, n):return await self._go("alt_names", [])
    async def fetch_redirect_sources(self, c, n):return await self._go("redirects", ["NaCl"])
    async def fetch_disambiguation_sources(self, c, n):return await self._go("disambiguations", [])

async def dig(tmp_path, stub):
    store = GraphStore(str(tmp_path / "g.json"))
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
                    "categories": ["Category:Deliquescent_materials"],
                    "max_depth": 0,
                },
            )
    finally:
        await http.aclose()


async def test_structure_query_failure_returns_incomplete_status(tmp_path):
    stub = Stub(fail={"subcategories"})
    response = await dig(tmp_path, stub)
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "incomplete"
    assert body["failed_requests"] == 1
    assert isinstance(body["error"], str)
    assert "1 SPARQL requests failed" in body["error"]

    # Confirm that the failing query was attempted.
    assert "subcategories" in stub.calls

    # The budget-exhausted header should be absent unless the budget
    # was actually exhausted.
    assert response.headers.get("x-sparql-budget-exhausted") is None
