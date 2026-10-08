"""
Offline API tests: request validation that is rejected BEFORE any SPARQL query
would be built. The app is wired to a client that fails the test if SPARQL is
touched, so these cannot silently depend on the network or on mock data.
"""
from app.config import settings


async def test_health_on_empty_graph(offline_api):
    response = await offline_api.get("/api/v1/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["graph_nodes"] == 0
    assert payload["graph_edges"] == 0


async def test_dig_rejects_plain_subject(offline_api):
    response = await offline_api.post(
        "/api/v1/dig", json={"categories": ["Food ingredients"], "max_depth": 0}
    )
    assert response.status_code == 400
    assert "not a category URL" in response.json()["detail"]


async def test_dig_rejects_max_depth_above_cap(offline_api):
    response = await offline_api.post(
        "/api/v1/dig",
        json={"categories": ["Category:Noble_gases"], "max_depth": settings.max_depth_hard_cap + 1},
    )
    assert response.status_code == 400
    assert "max_depth cannot exceed" in response.json()["detail"]


async def test_hydrate_synonyms_rejects_non_wikipedia_url(offline_api):
    response = await offline_api.post(
        "/api/v1/hydrate-synonyms",
        json={"entity": "https://example.com/wiki/Helium"},
    )
    assert response.status_code == 400
    assert "Unsupported resource URL" in response.json()["detail"]


async def test_dig_rejects_non_wikipedia_url(offline_api):
    response = await offline_api.post(
        "/api/v1/dig",
        json={"categories": ["https://example.com/wiki/Category:Noble_gases"], "max_depth": 0},
    )
    assert response.status_code == 400
    assert "Unsupported resource URL" in response.json()["detail"]
