"""
Offline /dig and /hydrate regressions: /dig hydrates every page it reaches,
/hydrate reports SPARQL call counts, and a SPARQL outage on the exists-check
(the one lookup /dig cannot skip) returns 502.
"""
import copy

import httpx
import pytest
from httpx import ASGITransport
from starlette.datastructures import State

from app.graph_store import GraphStore
from app.hydration import HydrationService
from app.main import app
from app.sparql_client import SparqlTransientError

CATEGORY = "Category:Noble_gases"


class StubSparqlClient:
    """In-process SPARQL stand-in. No network."""

    def __init__(self, *, exists: bool = True, error: Exception | None = None, pages: int = 0):
        self.exists = exists
        self.error = error
        self.pages = [f"Page_{i}" for i in range(pages)]
        self.calls = 0

    async def _maybe_fail(self):
        self.calls += 1
        if self.error is not None:
            raise self.error

    async def ask_category_exists(self, client, local_name):
        await self._maybe_fail()
        return self.exists

    async def fetch_subcategories(self, client, local_name):
        await self._maybe_fail()
        return []

    async def fetch_member_articles(self, client, local_name):
        await self._maybe_fail()
        return self.pages

    async def fetch_labels(self, client, local_name):
        await self._maybe_fail()
        return []

    async def fetch_alt_names(self, client, local_name):
        await self._maybe_fail()
        return []

    async def fetch_redirect_sources(self, client, local_name):
        await self._maybe_fail()
        return []

    async def fetch_disambiguation_sources(self, client, local_name):
        await self._maybe_fail()
        return []


@pytest.fixture
async def stub_api(tmp_path, request):
    sparql = request.param
    store = GraphStore(str(tmp_path / "graph_cache.json"))
    http_client = httpx.AsyncClient()
    local_app = copy.copy(app)
    local_app.state = State()
    local_app.state.store = store
    local_app.state.hydration = HydrationService(store, sparql, http_client)
    local_app.state.http_client = http_client
    async with httpx.AsyncClient(transport=ASGITransport(app=local_app), base_url="http://test") as client:
        yield client
    await http_client.aclose()


DIG_PAGES = 50


@pytest.mark.parametrize("stub_api", [StubSparqlClient(exists=True, pages=DIG_PAGES)], indirect=True)
async def test_dig_hydrates_every_page(stub_api, request):
    sparql = request.node.callspec.params["stub_api"]
    response = await stub_api.post(
        "/api/v1/dig",
        json={"categories": [CATEGORY], "max_depth": 1},
    )
    assert response.status_code == 200
    # exists ASK + sub-categories + member articles, then 4 synonym lookups per page
    assert sparql.calls == 3 + 4 * DIG_PAGES


@pytest.mark.parametrize("stub_api", [StubSparqlClient(exists=True)], indirect=True)
async def test_hydrate_synonyms_reports_calls_used(stub_api):
    response = await stub_api.post("/api/v1/hydrate-synonyms", json={"entity": "Helium"})
    assert response.status_code == 200
    body = response.json()
    # labels, alt names, redirects, disambiguations
    assert body["sparql_calls_used"] == 4


@pytest.mark.parametrize(
    "stub_api",
    [StubSparqlClient(error=SparqlTransientError("SPARQL endpoint down"))],
    indirect=True,
)
async def test_dig_sparql_outage_on_exists_check_returns_502(stub_api):
    response = await stub_api.post(
        "/api/v1/dig",
        json={"categories": [CATEGORY], "max_depth": 0},
    )
    assert response.status_code == 502
    assert "SPARQL endpoint down" in response.json()["detail"]
