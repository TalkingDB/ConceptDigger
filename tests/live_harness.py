"""
Live-SPARQL test harness.

Everything here talks to the *real* ConceptDigger code (the FastAPI app, the
HydrationService, the SparqlClient) and the *real* SPARQL endpoint. The only
addition is a thin recording layer so the HTML report can show exactly which
queries were sent and how the endpoint answered.

Parallel cases share one in-flight SPARQL lock. The public endpoint times out
when several reverse lookups (redirects / disambiguations) run at once.
"""
from __future__ import annotations

import asyncio
import contextvars
import copy
import json
import os
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from httpx import ASGITransport
from starlette.datastructures import State

from app.category import parse_category_url, parse_resource_url
from app.config import settings
from app.graph_store import GraphStore
from app.hydration import HydrationService
from app.main import app
from app.sparql_client import SparqlClient, SparqlTransientError

TESTS_DIR = Path(__file__).resolve().parent
DATA_DIR = TESTS_DIR / "debug_data"
INPUT_FILE = DATA_DIR / "input_data" / "wikipedia_categories.json"
EXPECTED_DIR = DATA_DIR / "expected_output"
RECORDS_DIR = TESTS_DIR / "records"


# ----------------------------------------------------------------------
# Failure taxonomy - the four outcomes the report distinguishes
# ----------------------------------------------------------------------
class LiveSparqlFailure(Exception):
    """The SPARQL endpoint / network misbehaved. Never a pass, never an assertion failure."""


class HarnessError(Exception):
    """The test itself could not run (bad data file, unexpected exception in app/harness)."""


class ExpectedOutputMismatch(AssertionError):
    """Actual output differs from the stored expected output."""


# ----------------------------------------------------------------------
# configuration
# ----------------------------------------------------------------------
def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise HarnessError(f"Environment variable {name}={raw!r} is not a number") from exc


@dataclass(frozen=True)
class LiveConfig:
    endpoint: str
    min_interval: float
    timeout: float
    max_retries: int
    concurrency: int
    time_budget: float

    @staticmethod
    def from_env() -> "LiveConfig":
        return LiveConfig(
            endpoint=settings.sparql_endpoint,  # SPARQL_ENDPOINT, same as the app
            min_interval=_env_float("TEST_SPARQL_MIN_INTERVAL_SECONDS", 0.1),
            timeout=_env_float("TEST_SPARQL_TIMEOUT_SECONDS", settings.sparql_timeout_seconds),
            max_retries=settings.sparql_max_retries,
            concurrency=max(1, int(_env_float("TEST_CONCURRENCY", 5))),
            time_budget=_env_float("TEST_SUITE_TIME_BUDGET_SECONDS", 600.0),
        )


# ----------------------------------------------------------------------
# test data
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class Case:
    id: str
    url: str
    max_depth: int
    return_categories: bool
    return_pages: bool
    description: str = ""
    # "category" digs a Wikipedia category. "article" hydrates one article's
    # label, alternate names, redirects and disambiguation text.
    kind: str = "category"

    @property
    def local_name(self) -> str:
        if self.kind == "article":
            return parse_resource_url(self.url)
        return parse_category_url(self.url)

    @property
    def request_body(self) -> dict:
        if self.kind == "article":
            return {"entity": self.url}
        return {
            "categories": [self.url],
            "max_depth": self.max_depth,
            "return_categories": self.return_categories,
            "return_pages": self.return_pages,
        }


def load_cases(path: Path = INPUT_FILE) -> list[Case]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        cases = []
        for c in raw["cases"]:
            kind = str(c.get("kind", "category"))
            if kind not in ("category", "article"):
                raise HarnessError(f"Input case {c.get('id')!r} has unknown kind {kind!r}")
            cases.append(
                Case(
                    id=str(c["id"]),
                    url=str(c["url"]),
                    max_depth=int(c.get("max_depth", 0)),
                    return_categories=bool(c.get("return_categories", True)),
                    return_pages=bool(c.get("return_pages", True)),
                    description=str(c.get("description", "")),
                    kind=kind,
                )
            )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise HarnessError(f"Cannot load input data {path}: {exc}") from exc
    return cases


def get_case(case_id: str) -> Case:
    for case in load_cases():
        if case.id == case_id:
            return case
    raise HarnessError(f"Input case '{case_id}' is missing from {INPUT_FILE}")


def load_expected(case_id: str) -> dict:
    path = EXPECTED_DIR / f"{case_id}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HarnessError(f"Cannot load expected output {path}: {exc}") from exc


# ----------------------------------------------------------------------
# recording SPARQL client
# ----------------------------------------------------------------------
@dataclass
class SparqlCall:
    kind: str
    query: str
    offset_s: float
    duration_s: float = 0.0
    result: str = ""
    http_statuses: list[int] = field(default_factory=list)
    error: str | None = None


_CURRENT_CALL: contextvars.ContextVar[SparqlCall | None] = contextvars.ContextVar(
    "current_sparql_call", default=None
)
# One live query at a time across every parallel case. The public endpoint
# queues or drops the rest, and those calls then fail the read timeout.
_BATCH_SPARQL_LOCK: contextvars.ContextVar[asyncio.Lock | None] = contextvars.ContextVar(
    "batch_sparql_lock", default=None
)


def _kind(query: str) -> str:
    q = query.lstrip()
    if q.upper().startswith("ASK"):
        return "ASK category exists"
    for needle, label in (
        ("wikiPageRedirects", "redirect sources"),
        ("wikiPageDisambiguates", "disambiguation sources"),
        ("dbo:alias", "alternative names"),
        ("rdfs:label", "labels (rdfs:label)"),
        ("dct:subject", "member articles (dct:subject)"),
        ("skos:broader", "sub-categories (skos:broader)"),
    ):
        if needle in q:
            return label
    return "other"


class RecordingSparqlClient(SparqlClient):
    """The production SparqlClient, plus a log of every logical query it ran."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls: list[SparqlCall] = []
        self._t0 = time.monotonic()

    async def _execute(self, client: httpx.AsyncClient, query: str) -> dict:
        call = SparqlCall(kind=_kind(query), query=query, offset_s=time.monotonic() - self._t0)
        token = _CURRENT_CALL.set(call)
        started = time.monotonic()
        lock = _BATCH_SPARQL_LOCK.get()
        try:
            if lock is None:
                payload = await super()._execute(client, query)
            else:
                async with lock:
                    payload = await super()._execute(client, query)
            if "boolean" in payload:
                call.result = f"boolean = {payload['boolean']}"
            else:
                call.result = f"{len(payload.get('results', {}).get('bindings', []))} row(s)"
            return payload
        except BaseException as exc:  # noqa: BLE001 - record then re-raise untouched
            call.error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            call.duration_s = time.monotonic() - started
            _CURRENT_CALL.reset(token)
            self.calls.append(call)

    @property
    def failed_calls(self) -> list[SparqlCall]:
        return [c for c in self.calls if c.error]


async def _record_http_status(response: httpx.Response) -> None:
    call = _CURRENT_CALL.get()
    if call is not None:
        call.http_statuses.append(response.status_code)


# ----------------------------------------------------------------------
# one isolated stack = own graph cache + own recorder + own copy of the real app
# ----------------------------------------------------------------------
class LiveStack:
    """
    Builds the real ConceptDigger app wired to the live endpoint.

    The app object is shallow-copied with a private `state`, so several stacks
    can run concurrently without sharing a graph cache or SPARQL counters.
    `interval_scale` multiplies the throttle so N concurrent stacks together
    stay at roughly TEST_SPARQL_MIN_INTERVAL_SECONDS between queries.
    """

    def __init__(self, cfg: LiveConfig, cache_dir: Path, interval_scale: int = 1):
        self.cfg = cfg
        self.cache_dir = Path(cache_dir)
        self.interval_scale = max(1, interval_scale)

    async def __aenter__(self) -> "LiveStack":
        self.store = GraphStore(str(self.cache_dir / "graph_cache.json"))
        self.recorder = RecordingSparqlClient(
            self.cfg.endpoint,
            self.cfg.timeout,
            min_interval=self.cfg.min_interval * self.interval_scale,
            max_retries=self.cfg.max_retries,
        )
        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(self.cfg.timeout, connect=10.0, pool=10.0),
            event_hooks={"response": [_record_http_status]},
        )
        self.hydration = HydrationService(self.store, self.recorder, self.http)
        self.app = copy.copy(app)
        self.app.state = State()
        self.app.state.store = self.store
        self.app.state.hydration = self.hydration
        self.app.state.http_client = self.http
        self.api = httpx.AsyncClient(
            transport=ASGITransport(app=self.app), base_url="http://concept-digger.test", timeout=None
        )
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.api.aclose()
        await self.http.aclose()


# ----------------------------------------------------------------------
# endpoint probe (one cheap live query before anything else)
# ----------------------------------------------------------------------
@dataclass
class ProbeResult:
    ok: bool
    endpoint: str
    duration_s: float = 0.0
    status: int | None = None
    error: str | None = None

    def require(self) -> None:
        if not self.ok:
            raise LiveSparqlFailure(
                f"SPARQL endpoint {self.endpoint} is not reachable/usable: {self.error}. "
                "Live tests cannot run; this is NOT a ConceptDigger assertion failure."
            )


def run_probe(cfg: LiveConfig) -> ProbeResult:
    last_error = None
    for attempt in range(2):
        started = time.monotonic()
        try:
            with httpx.Client(timeout=httpx.Timeout(min(cfg.timeout, 20.0), connect=10.0)) as client:
                resp = client.post(
                    cfg.endpoint,
                    data={"query": "ASK { ?s ?p ?o }", "format": "application/sparql-results+json"},
                    headers={"Accept": "application/sparql-results+json"},
                )
            duration = time.monotonic() - started
            if resp.status_code == 200 and resp.json().get("boolean") is True:
                return ProbeResult(True, cfg.endpoint, duration, resp.status_code)
            last_error = f"HTTP {resp.status_code}: {resp.text[:200]!r}"
            status = resp.status_code
        except (httpx.HTTPError, ValueError) as exc:
            duration = time.monotonic() - started
            last_error, status = f"{type(exc).__name__}: {exc}", None
        if attempt == 0:
            time.sleep(2.0)
    return ProbeResult(False, cfg.endpoint, duration, status, last_error)


# ----------------------------------------------------------------------
# running a dig case
# ----------------------------------------------------------------------
@dataclass
class CaseRun:
    case: Case
    status_code: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    body: Any = None
    raw_text: str = ""
    duration_s: float = 0.0
    offset_s: float = 0.0
    exception: str | None = None
    network_exception: bool = False
    sparql_calls: list[SparqlCall] = field(default_factory=list)


def is_network_exception(exc: BaseException) -> bool:
    return isinstance(exc, (SparqlTransientError, httpx.HTTPError))


async def attempt(coro) -> tuple[Any, str | None, bool]:
    """Await `coro`; return (result, traceback_text, is_network_failure)."""
    try:
        return await coro, None, False
    except Exception as exc:  # noqa: BLE001 - classified by the caller
        return None, traceback.format_exc(), is_network_exception(exc)


def synonym_records(local_name: str, graph_body: dict) -> list[dict]:
    """Turn one cached node's synonyms into the same record shape /dig returns."""
    nodes = graph_body.get("nodes") if isinstance(graph_body, dict) else None
    node = next((n for n in nodes or [] if n.get("id") == local_name), None)
    records = []
    for pair in (node or {}).get("synonyms") or []:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            continue
        text, source = pair
        records.append({
            "entity_url": f"DBPedia>{local_name}",
            "surface_text": text,
            "seed_category": local_name,
            "how_this_record": source,
        })
    return records


async def _call_case(stack: LiveStack, case: Case, run: CaseRun) -> None:
    if case.kind == "article":
        hyd = await stack.api.post("/api/v1/hydrate-synonyms", json=case.request_body)
        run.headers = {k: v for k, v in hyd.headers.items() if k.lower().startswith("x-sparql")}
        try:
            payload = hyd.json()
        except ValueError:
            run.status_code = hyd.status_code
            run.raw_text = hyd.text[:2000]
            return
        if hyd.status_code != 200:
            run.status_code = hyd.status_code
            run.body = payload
            return
        graph = await stack.api.get(
            "/api/v1/graph/data", params={"root": case.local_name, "depth": 0}
        )
        run.status_code = graph.status_code
        try:
            graph_body = graph.json()
        except ValueError:
            run.raw_text = graph.text[:2000]
            return
        run.body = synonym_records(case.local_name, graph_body) if graph.status_code == 200 else graph_body
        return

    resp = await stack.api.post("/api/v1/dig", json=case.request_body)
    run.status_code = resp.status_code
    run.headers = {k: v for k, v in resp.headers.items() if k.lower().startswith("x-sparql")}
    try:
        run.body = resp.json()
    except ValueError:
        run.raw_text = resp.text[:2000]


async def _run_case(cfg: LiveConfig, case: Case, cache_dir: Path, scale: int, t0: float,
                    gate: asyncio.Semaphore, sparql_lock: asyncio.Lock | None = None) -> CaseRun:
    run = CaseRun(case=case)
    lock_token = _BATCH_SPARQL_LOCK.set(sparql_lock) if sparql_lock is not None else None
    try:
        async with gate:
            run.offset_s = time.monotonic() - t0
            started = time.monotonic()
            try:
                async with LiveStack(cfg, cache_dir, scale) as stack:
                    try:
                        await _call_case(stack, case, run)
                    except Exception as exc:  # noqa: BLE001
                        run.exception = traceback.format_exc()
                        run.network_exception = is_network_exception(exc)
                    run.sparql_calls = list(stack.recorder.calls)
            except Exception:  # noqa: BLE001 - setup failure of the stack itself
                run.exception = traceback.format_exc()
            run.duration_s = time.monotonic() - started
        return run
    finally:
        if lock_token is not None:
            _BATCH_SPARQL_LOCK.reset(lock_token)


@dataclass
class DigBatch:
    runs: dict[str, CaseRun]
    wall_seconds: float


def run_dig_batch(cfg: LiveConfig, cases: list[Case], work_dir: Path) -> DigBatch:
    """Dig all `cases` concurrently against the live endpoint (each in its own stack)."""

    async def main() -> dict[str, CaseRun]:
        gate = asyncio.Semaphore(cfg.concurrency)
        scale = min(cfg.concurrency, max(1, len(cases)))
        t0 = time.monotonic()
        sparql_lock = asyncio.Lock()
        results = await asyncio.gather(
            *[
                _run_case(cfg, c, Path(work_dir) / c.id, scale, t0, gate, sparql_lock)
                for c in cases
            ]
        )
        return {r.case.id: r for r in results}

    started = time.monotonic()
    runs = asyncio.run(main()) if cases else {}
    return DigBatch(runs=runs, wall_seconds=time.monotonic() - started)


