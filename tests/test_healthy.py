import copy

import httpx
from httpx import ASGITransport
from starlette.datastructures import State

from app.graph_store import GraphStore
from app.hydration import HydrationService
from app.main import app
from app.sparql_client import SparqlTransientError


class Stub:
    """ASK says the category exists; methods named in `fail` raise like a DBpedia 503."""

    def __init__(self, fail=(), articles=("Sodium_chloride",)):
        self.fail, self.articles, self.calls = set(fail), list(articles), []

    async def _go(self, name, value):
        self.calls.append(name)
        if name in self.fail:
            raise SparqlTransientError("SPARQL endpoint returned HTTP 503")
        return value

    async def ask_category_exists(self, c, n): return await self._go("ask", True)
    async def fetch_subcategories(self, c, n): return await self._go("subcategories", [])
    async def fetch_member_articles(self, c, n): return await self._go("articles", self.articles)
    async def fetch_labels(self, c, n): return await self._go("labels", ["Sodium chloride"])
    async def fetch_alt_names(self, c, n): return await self._go("alt_names", [])
    async def fetch_redirect_sources(self, c, n): return await self._go("redirects", ["NaCl"])
    async def fetch_disambiguation_sources(self, c, n): return await self._go("disambiguations", [])


async def dig(tmp_path, stub):
    store, http = GraphStore(str(tmp_path / "g.json")), httpx.AsyncClient()
    a = copy.copy(app)
    a.state = State()
    a.state.store, a.state.http_client = store, http
    a.state.hydration = HydrationService(store, stub, http)
    async with httpx.AsyncClient(transport=ASGITransport(app=a), base_url="http://t") as c:
        r = await c.post("/api/v1/dig", json={"categories": ["Category:Deliquescent_materials"], "max_depth": 0})
    await http.aclose()
    return r


async def test_healthy(tmp_path):
    r = await dig(tmp_path, Stub())
    body = r.json()
    assert r.status_code == 200
    assert body["status"] == "complete"
    assert body["failed_requests"] == 0
    assert body["error"] is None
    assert {x["surface_text"] for x in body["data"]} == {
        "sodium chloride",
        "nacl",
    }
