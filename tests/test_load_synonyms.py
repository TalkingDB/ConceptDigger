"""
Load an export file into Elasticsearch: newest-first file list, Swagger dropdown,
append (no delete), and a response that says whether every synonym is searchable.
"""
import copy
import json
import os

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from starlette.datastructures import State

from app.export import export_dir, list_export_files
from app.graph_store import GraphStore
from app.main import app
from app.routers.synonyms import patch_openapi_file_enum

BEN = {
    "entity_url": "DBPedia>Ben_oil",
    "entity": "Ben_oil",
    "surface_text": [
        {"text": "ben oil", "how_this_record": "rdfs:label"},
        {"text": "behen oil", "how_this_record": "redirect"},
    ],
    "seed_category": "Category:Food_ingredients",
}
TURMERIC = {
    "entity_url": "DBPedia>Turmeric",
    "entity": "Turmeric",
    "surface_text": [{"text": "turmeric", "how_this_record": "rdfs:label"}],
    "seed_category": "Category:Spices",
}


class ElasticsearchMock:
    def __init__(self, *, fail=None, drop_text=None):
        self.fail = fail
        self.drop_text = drop_text
        self.index_exists = False
        self.docs = {}
        self.calls = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        if self.fail == "connect":
            raise httpx.ConnectError("connection refused")
        if self.fail == "timeout":
            raise httpx.ReadTimeout("timed out")
        if self.fail == "unavailable":
            return httpx.Response(503, json={"error": {"reason": "cluster not ready"}})

        path = request.url.path
        if request.method == "HEAD" and path.endswith("/train-synonyms"):
            return httpx.Response(200 if self.index_exists else 404)
        if request.method == "PUT" and path.endswith("/train-synonyms"):
            self.index_exists = True
            return httpx.Response(200, json={"acknowledged": True})
        if request.method == "GET" and path == "/":
            return httpx.Response(200, json={"tagline": "You Know, for Search"})
        if request.method == "POST" and path == "/_bulk":
            return self._bulk(request)
        if request.method == "POST" and path.endswith("/_refresh"):
            return httpx.Response(200, json={"_shards": {"failed": 0}})
        if request.method == "POST" and path.endswith("/_search"):
            return self._search(request)
        return httpx.Response(500, text=f"unexpected {request.method} {path}")

    def _bulk(self, request: httpx.Request) -> httpx.Response:
        lines = request.content.decode().splitlines()
        items = []
        for action_line, doc_line in zip(lines[0::2], lines[1::2]):
            action = json.loads(action_line)
            assert "index" in action and "delete" not in action
            doc_id = action["index"]["_id"]
            self.docs[doc_id] = json.loads(doc_line)
            items.append({"index": {"_id": doc_id, "status": 201, "result": "created"}})
        self.index_exists = True
        return httpx.Response(200, json={"errors": False, "items": items})

    def _search(self, request: httpx.Request) -> httpx.Response:
        wanted = json.loads(request.content)["query"]["ids"]["values"]
        hits = []
        for doc_id in wanted:
            source = dict(self.docs.get(doc_id) or {})
            if self.drop_text and source.get("surface_text"):
                source["surface_text"] = [
                    form for form in source["surface_text"] if form["text"] != self.drop_text
                ]
            if doc_id in self.docs:
                hits.append({"_id": doc_id, "_source": source})
        return httpx.Response(200, json={"hits": {"hits": hits}})


def _write(store: GraphStore, name: str, documents) -> None:
    directory = export_dir(store)
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, name), "w", encoding="utf-8") as handle:
        json.dump(documents, handle)


@pytest.fixture
def store(tmp_path):
    return GraphStore(str(tmp_path / "graph_cache.json"))


def test_export_files_are_newest_first_by_stamp(store):
    _write(store, "Food_20261021T153000.json", [])
    _write(store, "Food_20261023T010000.json", [])
    newer = os.path.join(export_dir(store), "Food_20261023T010000.json")
    older = os.path.join(export_dir(store), "Food_20261021T153000.json")
    os.utime(newer, (1, 1))
    os.utime(older, (2_000_000_000, 2_000_000_000))

    assert list_export_files(store) == [
        "Food_20261023T010000.json",
        "Food_20261021T153000.json",
    ]


def test_openapi_file_name_dropdown_is_newest_first(monkeypatch):
    names = ["Food_20261023T010000.json", "Food_20261021T153000.json"]
    monkeypatch.setattr("app.main.export_names_for_docs", lambda: names)

    schema = app.openapi()

    prop = schema["components"]["schemas"]["LoadSynonymsRequest"]["properties"]["file_name"]
    assert prop["enum"] == names


def test_openapi_without_exports_has_no_empty_enum():
    schema = {"components": {"schemas": {"LoadSynonymsRequest": {"properties": {"file_name": {}}}}}}
    patch_openapi_file_enum(schema, [])
    prop = schema["components"]["schemas"]["LoadSynonymsRequest"]["properties"]["file_name"]
    assert "enum" not in prop
    assert "No exports" in prop["description"]


@pytest_asyncio.fixture
async def synonyms_api(store):
    mock = ElasticsearchMock()
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(mock.handler))
    local_app = copy.copy(app)
    local_app.state = State()
    local_app.state.store = store
    local_app.state.http_client = http_client
    async with httpx.AsyncClient(transport=ASGITransport(app=local_app), base_url="http://test") as client:
        yield client, mock
    await http_client.aclose()


async def test_lists_exports_newest_first(synonyms_api, store):
    client, _ = synonyms_api
    _write(store, "Food_20261021T153000.json", [BEN])
    _write(store, "Spices_20261023T010000.json", [TURMERIC])

    response = await client.get("/api/v1/exports")

    assert response.status_code == 200
    assert response.json()["files"] == [
        "Spices_20261023T010000.json",
        "Food_20261021T153000.json",
    ]


async def test_rejects_a_path_and_a_missing_file(synonyms_api):
    client, _ = synonyms_api

    escaped = await client.post("/api/v1/synonyms/load", json={"file_name": "../secret.json"})
    missing = await client.post("/api/v1/synonyms/load", json={"file_name": "missing.json"})

    assert escaped.status_code == 400
    assert "not a path" in escaped.json()["detail"]
    assert missing.status_code == 404


async def test_load_reports_synonyms_ready_to_query(synonyms_api, store):
    client, mock = synonyms_api
    _write(store, "Food_20261023T010000.json", [BEN, TURMERIC])

    response = await client.post(
        "/api/v1/synonyms/load",
        json={"file_name": "Food_20261023T010000.json"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["elasticsearch_available"] is True
    assert body["ready_to_query"] is True
    assert body["documents_indexed"] == 2
    assert body["synonyms_indexed"] == 3
    assert body["synonyms_searchable"] == 3
    assert body["detail"] == "All synonyms are indexed and ready to query."
    assert ("DELETE", "/train-synonyms") not in mock.calls
    stored_texts = {
        form["text"]
        for doc in mock.docs.values()
        for form in doc["surface_text"]
    }
    assert stored_texts == {"ben oil", "behen oil", "turmeric"}


async def test_second_file_appends_and_keeps_earlier_synonyms(synonyms_api, store):
    client, mock = synonyms_api
    _write(store, "Food_20261021T153000.json", [BEN])
    _write(store, "Spices_20261023T010000.json", [TURMERIC])

    first = await client.post(
        "/api/v1/synonyms/load", json={"file_name": "Food_20261021T153000.json"},
    )
    second = await client.post(
        "/api/v1/synonyms/load", json={"file_name": "Spices_20261023T010000.json"},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert [method for method, path in mock.calls if method == "DELETE"] == []
    assert sum(1 for method, path in mock.calls if method == "PUT") == 1
    stored_texts = {
        form["text"]
        for doc in mock.docs.values()
        for form in doc["surface_text"]
    }
    assert stored_texts == {"ben oil", "behen oil", "turmeric"}
    assert len(mock.docs) == 2


async def test_elasticsearch_connection_failure_is_in_the_response(synonyms_api, store):
    client, mock = synonyms_api
    mock.fail = "connect"
    _write(store, "Food_20261023T010000.json", [BEN])

    response = await client.post(
        "/api/v1/synonyms/load",
        json={"file_name": "Food_20261023T010000.json"},
    )

    assert response.status_code == 503
    body = response.json()
    assert body["elasticsearch_available"] is False
    assert body["ready_to_query"] is False
    assert "not available" in body["detail"]
    assert "ConnectError" in body["detail"]
    assert "9200" in body["detail"]


async def test_elasticsearch_unavailable_status_is_in_the_response(synonyms_api, store):
    client, mock = synonyms_api
    mock.fail = "unavailable"
    _write(store, "Food_20261023T010000.json", [BEN])

    response = await client.post(
        "/api/v1/synonyms/load",
        json={"file_name": "Food_20261023T010000.json"},
    )

    assert response.status_code == 503
    body = response.json()
    assert body["elasticsearch_available"] is False
    assert body["ready_to_query"] is False
    assert "503" in body["detail"]
    assert "cluster not ready" in body["detail"]


async def test_missing_synonym_after_search_is_not_ready(synonyms_api, store):
    client, mock = synonyms_api
    mock.drop_text = "behen oil"
    _write(store, "Food_20261023T010000.json", [BEN])

    response = await client.post(
        "/api/v1/synonyms/load",
        json={"file_name": "Food_20261023T010000.json"},
    )

    assert response.status_code == 502
    body = response.json()
    assert body["elasticsearch_available"] is True
    assert body["ready_to_query"] is False
    assert body["synonyms_indexed"] == 2
    assert body["synonyms_searchable"] == 1
    assert "not ready to query" in body["detail"]
