"""Offline tests for opt-in /dig export, /export, record shape, and cleanup."""
import copy
import json
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


class StubSparqlClient:
    def __init__(self):
        self.calls = 0

    async def _count(self):
        self.calls += 1

    async def ask_category_exists(self, client, local_name):
        await self._count()
        return True

    async def fetch_subcategories(self, client, local_name):
        await self._count()
        return []

    async def fetch_member_articles(self, client, local_name):
        await self._count()
        return ["Helium"]

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
    store = GraphStore(str(tmp_path / "graph_cache.json"))
    http_client = httpx.AsyncClient()
    local_app = copy.copy(app)
    local_app.state = State()
    local_app.state.store = store
    local_app.state.hydration = HydrationService(store, StubSparqlClient(), http_client)
    local_app.state.http_client = http_client
    async with httpx.AsyncClient(transport=ASGITransport(app=local_app), base_url="http://test") as client:
        yield client, tmp_path, store
    await http_client.aclose()


def _exports(tmp_path: Path) -> list[Path]:
    folder = tmp_path / "exports"
    if not folder.exists():
        return []
    return sorted(folder.glob("*.json"))


async def test_dig_records_include_entity(stub_api):
    client, _tmp_path, _store = stub_api
    response = await client.post(
        "/api/v1/dig",
        json={"categories": [CATEGORY], "max_depth": 0},
    )
    assert response.status_code == 200
    records = response.json()
    assert records
    for rec in records:
        assert rec["entity"] == rec["entity_url"].removeprefix("DBPedia>")
        assert set(rec) == {
            "entity_url",
            "entity",
            "surface_text",
            "seed_category",
            "how_this_record",
        }


async def test_dig_does_not_export_by_default(stub_api):
    client, tmp_path, _store = stub_api
    response = await client.post(
        "/api/v1/dig",
        json={"categories": [CATEGORY], "max_depth": 0},
    )
    assert response.status_code == 200
    assert _exports(tmp_path) == []


async def test_dig_export_true_writes_file(stub_api):
    client, tmp_path, _store = stub_api
    response = await client.post(
        "/api/v1/dig",
        json={"categories": [CATEGORY], "max_depth": 0, "export": True},
    )
    assert response.status_code == 200
    files = _exports(tmp_path)
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload == response.json()
    assert all("entity" in rec for rec in payload)


async def test_dig_returns_records_when_export_write_fails(stub_api, monkeypatch):
    client, tmp_path, _store = stub_api

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
    client, tmp_path, _store = stub_api
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
    assert all("entity" in rec for rec in payload)


def test_write_exports_unique_filenames(tmp_path):
    store = GraphStore(str(tmp_path / "graph_cache.json"))
    records = [
        {
            "entity_url": "DBPedia>Helium",
            "entity": "Helium",
            "surface_text": "helium",
            "seed_category": CATEGORY,
            "how_this_record": "rdfs:label",
        }
    ]
    first = write_exports(store, [CATEGORY], records)
    second = write_exports(store, [CATEGORY], records)
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
    records = [
        {
            "entity_url": "DBPedia>Helium",
            "entity": "Helium",
            "surface_text": "helium",
            "seed_category": CATEGORY,
            "how_this_record": "rdfs:label",
        }
    ]
    written = write_exports(store, [CATEGORY], records)
    remaining = _exports(tmp_path)
    assert len(remaining) == keep
    assert Path(written[0]).exists()
