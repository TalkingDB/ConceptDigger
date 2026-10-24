
import copy
import httpx
from httpx import ASGITransport
from starlette.datastructures import State

from app.graph_store import GraphStore
from app.hydration import HydrationService
from app.main import app

class Stub:
    """Return successful, empty category results."""
    async def ask_category_exists(self, c, n):return True
    async def fetch_subcategories(self, c, n):return []
    async def fetch_member_articles(self, c, n):return []
    async def fetch_labels(self, c, n):return []
    async def fetch_alt_names(self, c, n):return []
    async def fetch_redirect_sources(self, c, n):return []
    async def fetch_disambiguation_sources(self, c, n):return []


async def dig(tmp_path, stub):
    store = GraphStore(str(tmp_path / "g.json"))
    http = httpx.AsyncClient()

    a = copy.copy(app)
    a.state = State()
    a.state.store = store
    a.state.http_client = http
    a.state.hydration = HydrationService(store, stub, http)

    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=a),
            base_url="http://t",
        ) as c:
            return await c.post(
                "/api/v1/dig",
                json={
                    "categories": ["Category:Deliquescent_materials"],
                    "max_depth": 0,
                },
            )
    finally:
        await http.aclose()


async def test_really_empty_returns_complete_status(tmp_path):
    response = await dig(tmp_path, Stub())
    body = response.json()

    assert response.status_code == 200
    assert body["data"] == []
    assert body["status"] == "complete"
    assert body["failed_requests"] == 0
    assert body["error"] is None
