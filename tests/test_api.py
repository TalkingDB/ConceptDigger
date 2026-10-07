"""Functional API tests against a mocked SPARQL endpoint."""
from dataclasses import replace
from unittest.mock import patch

from app.config import settings
from tests.conftest import canonical_graph_payload, load_golden, sorted_dig_records


def _assert_dig_matches_golden(body, golden_name: str) -> None:
    assert sorted_dig_records(body) == sorted_dig_records(load_golden(golden_name))


async def test_health_on_empty_graph(api_client):
    response = await api_client.get("/api/v1/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["graph_nodes"] == 0
    assert payload["graph_edges"] == 0


async def test_dig_rejects_plain_subject(api_client):
    response = await api_client.post(
        "/api/v1/dig",
        json={"categories": ["Food ingredients"], "max_depth": 0},
    )
    assert response.status_code == 400
    assert "not a category URL" in response.json()["detail"]


async def test_dig_rejects_max_depth_above_cap(api_client):
    response = await api_client.post(
        "/api/v1/dig",
        json={
            "categories": ["Category:Test_food"],
            "max_depth": settings.max_depth_hard_cap + 1,
        },
    )
    assert response.status_code == 400
    assert "max_depth cannot exceed" in response.json()["detail"]


async def test_dig_unknown_category_is_404(api_client):
    response = await api_client.post(
        "/api/v1/dig",
        json={"categories": ["Category:Does_Not_Exist"], "max_depth": 0},
    )
    assert response.status_code == 404
    assert "Category not found" in response.json()["detail"]


async def test_dig_accepts_wikipedia_url_and_matches_golden_depth0(api_client):
    response = await api_client.post(
        "/api/v1/dig",
        json={
            "categories": ["https://en.wikipedia.org/wiki/Category:Test_food"],
            "max_depth": 0,
            "return_categories": True,
            "return_pages": True,
        },
    )
    assert response.status_code == 200
    _assert_dig_matches_golden(response.json(), "dig_depth0.json")


async def test_dig_depth1_includes_nested_article(api_client):
    response = await api_client.post(
        "/api/v1/dig",
        json={
            "categories": ["Category:Test_food"],
            "max_depth": 1,
            "return_categories": True,
            "return_pages": True,
        },
    )
    assert response.status_code == 200
    _assert_dig_matches_golden(response.json(), "dig_depth1.json")


async def test_dig_pages_only_omits_categories(api_client):
    response = await api_client.post(
        "/api/v1/dig",
        json={
            "categories": ["Category:Test_food"],
            "max_depth": 0,
            "return_categories": False,
            "return_pages": True,
        },
    )
    assert response.status_code == 200
    _assert_dig_matches_golden(response.json(), "dig_pages_only.json")


async def test_dig_categories_only_omits_pages(api_client):
    response = await api_client.post(
        "/api/v1/dig",
        json={
            "categories": ["Category:Test_food"],
            "max_depth": 0,
            "return_categories": True,
            "return_pages": False,
        },
    )
    assert response.status_code == 200
    _assert_dig_matches_golden(response.json(), "dig_categories_only.json")


async def test_dig_sets_budget_exhausted_header(api_client):
    tight = replace(settings, max_sparql_calls_per_request=1)
    with patch("app.routers.dig.settings", tight), patch("app.dig.settings", tight):
        response = await api_client.post(
            "/api/v1/dig",
            json={"categories": ["Category:Test_food"], "max_depth": 1},
        )
    assert response.status_code == 200
    assert response.headers.get("x-sparql-budget-exhausted") == "true"


async def test_hydrate_category_then_graph_data_matches_golden(api_client):
    empty = await api_client.get(
        "/api/v1/graph/data",
        params={"root": "Category:Test_food", "depth": 2},
    )
    assert empty.status_code == 200
    empty_payload = empty.json()
    assert empty_payload["nodes"] == []
    assert empty_payload["edges"] == []
    assert "not found" in empty_payload["note"]

    warmed = await api_client.post(
        "/api/v1/hydrate-category",
        json={"category": "Category:Test_food", "max_depth": 2},
    )
    assert warmed.status_code == 200
    body = warmed.json()
    assert body["category"] == "Category:Test_food"
    assert body["max_depth"] == 2
    assert body["new_nodes"] == 4
    assert body["new_edges"] == 3
    assert body["budget_exhausted"] is False

    graph = await api_client.get(
        "/api/v1/graph/data",
        params={"root": "Category:Test_food", "depth": 2},
    )
    assert graph.status_code == 200
    expected = load_golden("graph_data_after_hydrate.json")
    assert canonical_graph_payload(graph.json()) == canonical_graph_payload(expected)


async def test_hydrate_synonyms_adds_alt_names(api_client):
    await api_client.post(
        "/api/v1/hydrate-category",
        json={"category": "Category:Test_food", "max_depth": 1},
    )
    response = await api_client.post(
        "/api/v1/hydrate-synonyms",
        json={"entity": "Cinnamon"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["entity"] == "Cinnamon"
    assert payload["synonyms_added"] >= 1
    assert payload["budget_exhausted"] is False

    graph = await api_client.get(
        "/api/v1/graph/data",
        params={"root": "Category:Test_food", "depth": 1},
    )
    cinnamon = next(n for n in graph.json()["nodes"] if n["id"] == "Cinnamon")
    texts = {pair[0] for pair in cinnamon["synonyms"]}
    assert "cinnamon" in texts
    assert "ceylon cinnamon" in texts
    assert "cinnamomum verum" in texts
    assert cinnamon["synonyms_hydrated"] is True


async def test_health_counts_after_hydration(api_client):
    await api_client.post(
        "/api/v1/hydrate-category",
        json={"category": "Category:Test_food", "max_depth": 2},
    )
    response = await api_client.get("/api/v1/health")
    assert response.json() == {
        "status": "ok",
        "graph_nodes": 4,
        "graph_edges": 3,
    }
