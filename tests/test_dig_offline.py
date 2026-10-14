"""
Offline /dig regressions that used to live in the mocked API suite:
per-request SPARQL budget exhaustion, and a SPARQL outage on the
exists-check (the one lookup /dig cannot skip).
"""
import copy
from dataclasses import replace
from unittest.mock import patch

import httpx
import pytest
from httpx import ASGITransport
from starlette.datastructures import State

from app.config import settings
from app.graph_store import GraphStore
from app.hydration import HydrationService
from app.main import app
from app.sparql_client import SparqlTransientError

CATEGORY = "Category:Noble_gases"


class StubSparqlClient:
    """In-process SPARQL stand-in. No network."""

    def __init__(self, *, exists: bool = True, error: Exception | None = None):
        self.exists = exists
        self.error = error

    async def _maybe_fail(self):
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
        return []

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


@pytest.mark.parametrize("stub_api", [StubSparqlClient(exists=True)], indirect=True)
async def test_dig_sets_budget_exhausted_header(stub_api):
    tight = replace(settings, max_sparql_calls_per_request=1)
    with patch("app.routers.dig.settings", tight), patch("app.dig.settings", tight):
        response = await stub_api.post(
            "/api/v1/dig",
            json={"categories": [CATEGORY], "max_depth": 1},
        )
    assert response.status_code == 200
    assert response.headers.get("x-sparql-budget-exhausted") == "true"
    assert response.json() == []


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
