"""Offline tests for opt-in /dig export, /export, record shape, and cleanup."""
import copy
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport
from starlette.datastructures import State

from app.config import settings
from app.export import write_exports
from app.graph_store import GraphStore
from app.hydration import HydrationService
from app.main import app

CATEGORY = "Category:Noble_gases"
RECORD = {
    "entity_url": "DBPedia>Helium",
    "surface_text": "helium",
    "seed_category": CATEGORY,
    "how_this_record": "rdfs:label",
}

TREE = {
    "Category:Root": {"subcats": ["Category:Child"], "pages": ["ShallowPage"]},
    "Category:Child": {"subcats": [], "pages": ["DeepPage"]},
}

MULTI = {
    "Category:Gases": {"subcats": [], "pages": ["Helium"]},
    "Category:Metals": {"subcats": [], "pages": ["Iron"]},
}


class StubSparqlClient:
    def __init__(self, tree=None):
        self.tree = tree
        self.calls = 0

    async def _count(self):
        self.calls += 1

    async def ask_category_exists(self, client, local_name):
        await self._count()
        return True

    async def fetch_subcategories(self, client, local_name):
        await self._count()
        if self.tree is None:
            return []
        return self.tree.get(local_name, {}).get("subcats", [])

    async def fetch_member_articles(self, client, local_name):
        await self._count()
        if self.tree is None:
            return ["Helium"]
        return self.tree.get(local_name, {}).get("pages", [])

    async def fetch_labels(self, client, local_name):
        await self._count()
        return [local_name.replace("_", " ").lower()]

    async def fetch_alt_names(self, client, local_name):
        await self._count()
        return []

    async def fetch_redirect_sources(self, client, local_name):
        await self._count()
        return []

    async def fetch_disambiguation_sources(self, client, local_name):
        await self._count()
        return []


@pytest.fixture
async def stub_api(tmp_path):
    async with _app_client(tmp_path) as packed:
        yield packed


@pytest.fixture
async def tree_api(tmp_path):
    async with _app_client(tmp_path, TREE) as packed:
        yield packed


@pytest.fixture
async def multi_api(tmp_path):
    async with _app_client(tmp_path, MULTI) as packed:
        yield packed


class _app_client:
    def __init__(self, tmp_path, tree=None):
        self.tmp_path = tmp_path
        self.sparql = StubSparqlClient(tree)
        self.store = GraphStore(str(tmp_path / "graph_cache.json"))
        self.http_client = httpx.AsyncClient()
        local_app = copy.copy(app)
        local_app.state = State()
        local_app.state.store = self.store
        local_app.state.hydration = HydrationService(self.store, self.sparql, self.http_client)
        local_app.state.http_client = self.http_client
        self._transport = httpx.AsyncClient(transport=ASGITransport(app=local_app), base_url="http://test")

    async def __aenter__(self):
        self.client = await self._transport.__aenter__()
        return self.client, self.tmp_path, self.store, self.sparql

    async def __aexit__(self, exc_type, exc, tb):
        await self._transport.__aexit__(exc_type, exc, tb)
        await self.http_client.aclose()


def _exports(tmp_path: Path) -> list[Path]:
    folder = tmp_path / "exports"
    if not folder.exists():
        return []
    return sorted(folder.glob("*.json"))


def _record_key(rec: dict) -> tuple:
    return (rec["entity_url"], rec["surface_text"], rec["how_this_record"], rec["seed_category"])


async def test_dig_records_include_entity(stub_api):
    client, _tmp_path, _store, _sparql = stub_api
    response = await client.post(
        "/api/v1/dig",
        json={"categories": [CATEGORY], "max_depth": 0},
    )
    assert response.status_code == 200
    response_json = response.json()
    records = response_json.get("data", [])
    assert records
    for rec in records:
        assert rec["entity_url"] == rec["entity_url"]
        assert set(rec) == {
            "entity_url",
            "surface_text",
            "seed_category",
            "how_this_record",
        }


async def test_dig_does_not_export_by_default(stub_api):
    client, tmp_path, _store, _sparql = stub_api
    response = await client.post(
        "/api/v1/dig",
        json={"categories": [CATEGORY], "max_depth": 0},
    )
    assert response.status_code == 200
    assert _exports(tmp_path) == []


async def test_dig_export_true_writes_file(stub_api):
    client, tmp_path, _store, _sparql = stub_api
    response = await client.post(
        "/api/v1/dig",
        json={"categories": [CATEGORY], "max_depth": 0, "export": True},
    )
    assert response.status_code == 200
    files = _exports(tmp_path)
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    digged_data = response.json()
    assert payload == digged_data["data"]
    assert all("entity_url" in rec for rec in payload)


async def test_dig_returns_records_when_export_write_fails(stub_api, monkeypatch):
    client, tmp_path, _store, _sparql = stub_api

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("app.routers.dig.write_exports", boom)
    response = await client.post(
        "/api/v1/dig",
        json={"categories": [CATEGORY], "max_depth": 0, "export": True},
    )
    assert response.status_code == 200
    assert response.json()
    assert _exports(tmp_path) == []


async def test_export_endpoint_writes_file(stub_api):
    client, tmp_path, _store, _sparql = stub_api
    await client.post("/api/v1/dig", json={"categories": [CATEGORY], "max_depth": 0})
    response = await client.post(
        "/api/v1/export",
        json={"category": CATEGORY, "max_depth": 0},
    )
    assert response.status_code == 200
    files = response.json()["files"]
    assert len(files) == 1
    assert Path(files[0]).exists()
    payload = json.loads(Path(files[0]).read_text(encoding="utf-8"))
    assert all("entity_url" in rec for rec in payload)


async def test_export_404_for_uncached_category(offline_api):
    response = await offline_api.post(
        "/api/v1/export",
        json={"category": CATEGORY, "max_depth": 0},
    )
    assert response.status_code == 404
    assert "not in cache" in response.json()["detail"]


async def test_export_400_for_invalid_category(offline_api):
    response = await offline_api.post(
        "/api/v1/export",
        json={"category": "Food ingredients", "max_depth": 0},
    )
    assert response.status_code == 400
    assert "not a category URL" in response.json()["detail"]


async def test_export_400_for_excessive_max_depth(offline_api):
    response = await offline_api.post(
        "/api/v1/export",
        json={"category": CATEGORY, "max_depth": settings.max_depth_hard_cap + 1},
    )
    assert response.status_code == 400
    assert "max_depth cannot exceed" in response.json()["detail"]


async def test_export_matches_dig_for_nested_cache(tree_api):
    client, _tmp_path, _store, sparql = tree_api
    res = await client.post(
        "/api/v1/dig",
        json={"categories": ["Category:Root"], "max_depth": 1},
    )
    assert res.status_code == 200
    response = res.json()
    dig_records = response.get("data", [])
    entities = {r["entity_url"] for r in dig_records}
    assert "DBPedia>Category:Child" in entities
    assert "DBPedia>ShallowPage" in entities
    assert "DBPedia>DeepPage" in entities

    calls_after_dig = sparql.calls
    exported = await client.post(
        "/api/v1/export",
        json={"category": "Category:Root", "max_depth": 1},
    )
    assert exported.status_code == 200
    assert sparql.calls == calls_after_dig

    files = exported.json()["files"]
    assert len(files) == 1
    export_records = json.loads(Path(files[0]).read_text(encoding="utf-8"))
    shared_fields = {
        "entity_url",
        "surface_text",
        "seed_category",
        "how_this_record",
    }

    normalized_export = [
        {key: record[key] for key in shared_fields if key in record}
        for record in export_records
    ]

    normalized_dig = [
        {key: record[key] for key in shared_fields if key in record}
        for record in dig_records
    ]

    assert sorted(normalized_export, key=_record_key) == sorted(normalized_dig, key=_record_key)


async def test_multiple_seeds_write_separate_files(multi_api):
    client, tmp_path, _store, _sparql = multi_api
    response = await client.post(
        "/api/v1/dig",
        json={
            "categories": ["Category:Gases", "Category:Metals"],
            "max_depth": 0,
            "export": True,
        },
    )
    assert response.status_code == 200
    files = _exports(tmp_path)
    assert len(files) == 2
    by_seed = {}
    for path in files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        seeds = {rec["seed_category"] for rec in payload}
        assert len(seeds) == 1
        seed = seeds.pop()
        by_seed[seed] = {rec["entity_url"] for rec in payload}
    assert by_seed["Category:Gases"] == {"DBPedia>Helium"}
    assert by_seed["Category:Metals"] == {"DBPedia>Iron"}


def test_write_exports_unique_filenames(tmp_path):
    store = GraphStore(str(tmp_path / "graph_cache.json"))
    first = write_exports(store, [CATEGORY], [RECORD])
    second = write_exports(store, [CATEGORY], [RECORD])
    assert first[0] != second[0]
    assert Path(first[0]).exists()
    assert Path(second[0]).exists()


def test_write_exports_prunes_old_files(tmp_path):
    store = GraphStore(str(tmp_path / "graph_cache.json"))
    folder = tmp_path / "exports"
    folder.mkdir()
    keep = settings.export_max_files
    for i in range(keep + 5):
        (folder / f"old_{i:03d}.json").write_text("[]", encoding="utf-8")
    written = write_exports(store, [CATEGORY], [RECORD])
    remaining = _exports(tmp_path)
    assert len(remaining) == keep
    assert Path(written[0]).exists()


def test_pruning_disabled_never_deletes_files(tmp_path, monkeypatch):
    monkeypatch.setattr("app.export.settings", replace(settings, export_max_files=0))
    store = GraphStore(str(tmp_path / "graph_cache.json"))
    folder = tmp_path / "exports"
    folder.mkdir()
    old = [folder / f"old_{i:03d}.json" for i in range(5)]
    for path in old:
        path.write_text("[]", encoding="utf-8")
    written = write_exports(store, [CATEGORY], [RECORD])
    remaining = set(_exports(tmp_path))
    assert Path(written[0]) in remaining
    assert remaining.issuperset(old)
    assert len(remaining) == 6
