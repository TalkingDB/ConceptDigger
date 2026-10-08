"""Shared fixtures, mocked SPARQL wiring, and test-record writers."""
from __future__ import annotations

import copy
import tempfile
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from starlette.datastructures import State

from app.graph_store import GraphStore
from app.hydration import HydrationService
from app.main import app
from tests import reporting
from tests.live_harness import (
    RECORDS_DIR,
    CaseRun,
    DigBatch,
    HarnessError,
    LiveConfig,
    LiveStack,
    ProbeResult,
    load_cases,
    run_dig_batch,
    run_probe,
)


def pytest_configure(config):
    config.addinivalue_line("markers", "live: queries the live SPARQL endpoint (network access required)")
    try:
        reporting.STATE.cases = load_cases()
    except HarnessError:
        reporting.STATE.cases = []  # surfaced as a clear error by the data-integrity tests
    config.pluginmanager.register(reporting.LiveReporting(RECORDS_DIR), "concept-digger-live-reporting")


@pytest.fixture(scope="session")
def live_config() -> LiveConfig:
    cfg = LiveConfig.from_env()
    reporting.STATE.cfg = cfg
    return cfg


@pytest.fixture(scope="session")
def sparql_probe(live_config) -> ProbeResult:
    """One cheap live query up front. Tests call .require() so a dead endpoint
    shows up as SPARQL/NETWORK FAILURE in every live test (never as a pass)."""
    probe = run_probe(live_config)
    reporting.STATE.probe = probe
    return probe


@pytest.fixture(scope="session")
def live_dig_results(request, live_config, sparql_probe) -> DigBatch:
    """Dig every collected input category against the live endpoint, in parallel."""
    wanted = []
    seen = set()
    for item in request.session.items:
        callspec = getattr(item, "callspec", None)
        case = callspec.params.get("case") if callspec else None
        if case is not None and hasattr(case, "request_body") and case.id not in seen:
            seen.add(case.id)
            wanted.append(case)
    if not sparql_probe.ok:
        batch = DigBatch(runs={c.id: CaseRun(case=c) for c in wanted}, wall_seconds=0.0)
    else:
        with tempfile.TemporaryDirectory(prefix="concept-digger-live-") as tmp:
            batch = run_dig_batch(live_config, wanted, Path(tmp))
    reporting.STATE.batch = batch
    return batch


@pytest_asyncio.fixture
async def live_stack(live_config, tmp_path):
    """Real app + real live SPARQL client with its own empty graph cache."""
    async with LiveStack(live_config, tmp_path) as stack:
        yield stack


class _ForbiddenSparqlClient:
    def __getattr__(self, name):
        raise AssertionError(f"offline test tried to query SPARQL ({name}); use a live test instead")


@pytest_asyncio.fixture
async def offline_api(tmp_path):
    store = GraphStore(str(tmp_path / "graph_cache.json"))
    http_client = httpx.AsyncClient()
    local_app = copy.copy(app)
    local_app.state = State()
    local_app.state.store = store
    local_app.state.hydration = HydrationService(store, _ForbiddenSparqlClient(), http_client)
    local_app.state.http_client = http_client
    async with httpx.AsyncClient(transport=ASGITransport(app=local_app), base_url="http://test") as client:
        yield client
    await http_client.aclose()
