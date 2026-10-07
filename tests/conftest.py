"""Shared fixtures, mocked SPARQL wiring, and test-record writers."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from app.graph_store import GraphStore
from app.hydration import HydrationService
from app.main import app
from tests.mock_sparql import MockSparqlClient

TEST_DIR = Path(__file__).resolve().parent
TEST_DATA_DIR = TEST_DIR / "test_data"
GOLDEN_DIR = TEST_DATA_DIR / "golden"
RECORDS_DIR = TEST_DIR / "records"
HISTORY_DIR = RECORDS_DIR / "history"
FIXTURE_PATH = TEST_DATA_DIR / "sparql_fixture.json"

_reports: list[dict] = []


def load_sparql_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def load_golden(name: str):
    path = GOLDEN_DIR / name
    return json.loads(path.read_text(encoding="utf-8"))


def sorted_dig_records(records: list[dict]) -> list[dict]:
    return sorted(
        records,
        key=lambda r: (
            r.get("entity_url", ""),
            r.get("surface_text", ""),
            r.get("seed_category", ""),
            r.get("how_this_record", ""),
        ),
    )


def canonical_graph_payload(payload: dict) -> dict:
    nodes = sorted(payload.get("nodes", []), key=lambda n: n.get("id", ""))
    edges = sorted(
        payload.get("edges", []),
        key=lambda e: (e.get("source", ""), e.get("target", "")),
    )
    out = {
        "root": payload.get("root"),
        "depth": payload.get("depth"),
        "nodes": nodes,
        "edges": edges,
    }
    if "note" in payload:
        out["note"] = payload["note"]
    return out


def pytest_configure(config):
    RECORDS_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)


def pytest_runtest_logreport(report):
    if report.when != "call":
        return
    _reports.append(
        {
            "nodeid": report.nodeid,
            "outcome": report.outcome,
            "duration": report.duration,
            "longrepr": str(report.longrepr) if report.failed else None,
        }
    )


def pytest_sessionfinish(session, exitstatus):
    RECORDS_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    counts = {"passed": 0, "failed": 0, "skipped": 0, "error": 0}
    for item in _reports:
        counts[item["outcome"]] = counts.get(item["outcome"], 0) + 1
    summary = {
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "exitstatus": int(exitstatus),
        "python": sys.version,
        "counts": counts,
        "tests": _reports,
    }
    latest = RECORDS_DIR / "summary.json"
    latest.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (HISTORY_DIR / f"summary-{stamp}.json").write_text(
        json.dumps(summary, indent=2),
        encoding="utf-8",
    )


@pytest_asyncio.fixture
async def api_client(tmp_path):
    store = GraphStore(str(tmp_path / "graph_cache.json"))
    http_client = httpx.AsyncClient()
    hydration = HydrationService(store, MockSparqlClient(load_sparql_fixture()), http_client)
    app.state.store = store
    app.state.hydration = hydration
    app.state.http_client = http_client
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    await http_client.aclose()
