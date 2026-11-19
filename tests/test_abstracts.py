"""
Offline tests for page abstracts (Wikipedia intro text):
  - WikipediaClient: title normalization/redirect mapping, missing and
    disambiguation pages, "continue" paging, retries, User-Agent.
  - HydrationService.hydrate_abstracts: categories skipped, batching,
    transient failures left for retry, refresh.
  - API: /dig fills abstracts, /graph/data returns them, /hydrate-abstracts
    backfills an already-cached graph.
"""
import copy
import json

import httpx
import pytest
from httpx import ASGITransport
from starlette.datastructures import State

from app.graph_store import GraphStore
from app.hydration import HydrationService
from app.main import app
from app.wikipedia_client import WikipediaClient, WikipediaTransientError

UA = "ConceptDigger-test/1.0 (https://example.invalid/repo)"


def make_client(max_retries: int = 2) -> WikipediaClient:
    client = WikipediaClient("https://wiki.test/w/api.php", UA, timeout=5, max_retries=max_retries)

    async def no_sleep(*_args, **_kwargs):
        return None

    client._backoff = no_sleep
    return client


def wiki_response(pages, normalized=(), redirects=(), cont=None):
    body = {"batchcomplete": True, "query": {"pages": list(pages)}}
    if normalized:
        body["query"]["normalized"] = [{"from": f, "to": t} for f, t in normalized]
    if redirects:
        body["query"]["redirects"] = [{"from": f, "to": t} for f, t in redirects]
    if cont:
        body["continue"] = cont
    return httpx.Response(200, json=body)


# ----------------------------------------------------------------------
# WikipediaClient
# ----------------------------------------------------------------------
async def test_fetch_maps_normalized_redirected_missing_and_disambiguation_pages():
    seen = {}

    def handler(request: httpx.Request):
        seen["ua"] = request.headers["user-agent"]
        seen["titles"] = request.url.params["titles"]
        return wiki_response(
            [
                {"title": "Maida (flour)", "extract": "Maida is a type of  wheat flour.\n\nIt is refined."},
                {"title": "Citric acid", "extract": "Citric acid is an organic compound."},
                {"title": "Mercury", "extract": "Mercury may refer to:", "pageprops": {"disambiguation": ""}},
                {"title": "No such page", "missing": True},
                {"title": "Empty page", "extract": ""},
            ],
            normalized=[("Maida_(flour)", "Maida (flour)"), ("E330", "E330"), ("No_such_page", "No such page"),
                        ("Empty_page", "Empty page")],
            redirects=[("E330", "Citric acid")],
        )

    client = make_client()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await client.fetch_abstracts(
            http, ["Maida_(flour)", "E330", "Mercury", "No_such_page", "Empty_page"]
        )

    assert seen["ua"] == UA
    assert seen["titles"] == "Maida_(flour)|E330|Mercury|No_such_page|Empty_page"
    assert result == {
        "Maida_(flour)": "Maida is a type of wheat flour. It is refined.",  # whitespace collapsed
        "E330": "Citric acid is an organic compound.",  # followed the redirect
        "Mercury": None,  # disambiguation page describes no one entity
        "No_such_page": None,
        "Empty_page": None,
    }


async def test_fetch_decodes_percent_encoded_names():
    # DBpedia local names percent-encode some characters; Wikipedia needs the real title.
    seen = {}

    def handler(request: httpx.Request):
        seen["titles"] = request.url.params["titles"]
        return wiki_response(
            [{"title": "Got Rice?", "extract": "Got Rice? is a slogan."},
             {"title": "Chivita 100%", "extract": "Chivita 100% is a juice."}],
            normalized=[("Got_Rice?", "Got Rice?"), ("Chivita_100%", "Chivita 100%")],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await make_client().fetch_abstracts(http, ["Got_Rice%3F", "Chivita_100%25"])

    assert seen["titles"] == "Got_Rice?|Chivita_100%"
    assert result == {"Got_Rice%3F": "Got Rice? is a slogan.", "Chivita_100%25": "Chivita 100% is a juice."}


async def test_fetch_follows_continue_and_merges_partial_pages():
    calls = []

    def handler(request: httpx.Request):
        calls.append(dict(request.url.params))
        if "excontinue" not in request.url.params:
            # First response: extract only for A; B comes in the continuation.
            return wiki_response(
                [{"title": "A", "extract": "A is a thing."}, {"title": "B"}],
                cont={"excontinue": "1", "continue": "||"},
            )
        return wiki_response([{"title": "A"}, {"title": "B", "extract": "B is another thing."}])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await make_client().fetch_abstracts(http, ["A", "B"])

    assert len(calls) == 2
    assert calls[1]["excontinue"] == "1"
    assert result == {"A": "A is a thing.", "B": "B is another thing."}


async def test_fetch_retries_rate_limit_then_succeeds():
    responses = iter([httpx.Response(429), httpx.Response(503), wiki_response([{"title": "A", "extract": "A."}])])
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: next(responses))) as http:
        result = await make_client(max_retries=2).fetch_abstracts(http, ["A"])
    assert result == {"A": "A."}


async def test_fetch_raises_transient_after_retries():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503))) as http:
        with pytest.raises(WikipediaTransientError):
            await make_client(max_retries=1).fetch_abstracts(http, ["A"])


async def test_fetch_api_error_is_transient():
    error = httpx.Response(200, json={"error": {"code": "internal_api_error", "info": "boom"}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: error)) as http:
        with pytest.raises(WikipediaTransientError):
            await make_client().fetch_abstracts(http, ["A"])


async def test_fetch_rejects_more_than_20_titles():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as http:
        with pytest.raises(ValueError):
            await make_client().fetch_abstracts(http, [f"P{i}" for i in range(21)])


# ----------------------------------------------------------------------
# HydrationService.hydrate_abstracts
# ----------------------------------------------------------------------
class StubWikipedia:
    """Returns "<name> abstract." for every title, except names in `no_abstract`;
    raises for any batch containing a name in `fail_on`."""

    def __init__(self, no_abstract=(), fail_on=()):
        self.no_abstract = set(no_abstract)
        self.fail_on = set(fail_on)
        self.batches = []

    async def fetch_abstracts(self, client, names):
        names = list(names)
        self.batches.append(names)
        if self.fail_on & set(names):
            raise WikipediaTransientError("Wikipedia down")
        return {n: (None if n in self.no_abstract else f"{n} abstract.") for n in names}


def seeded_store(tmp_path, pages):
    store = GraphStore(str(tmp_path / "graph_cache.json"))
    store.ensure_node("Category:Food", "rdfs:label")
    for page in pages:
        store.ensure_node(page, "rdfs:label")
        store.ensure_edge("Category:Food", page)
    return store


async def test_hydrate_abstracts_batches_pages_and_skips_categories(tmp_path):
    pages = [f"Page_{i}" for i in range(45)]
    store = seeded_store(tmp_path, pages)
    wiki = StubWikipedia(no_abstract={"Page_3"})
    hydration = HydrationService(store, sparql_client=None, http_client=None, wikipedia_client=wiki)

    report = await hydration.hydrate_abstracts(["Category:Food", "Unknown_node", *pages])

    assert [len(b) for b in wiki.batches] == [20, 20, 5]
    assert all(not name.startswith("Category:") for b in wiki.batches for name in b)
    assert (report.requested, report.with_abstract, report.without_abstract, report.failed) == (45, 44, 1, 0)
    assert report.wikipedia_calls_used == 3
    assert store.graph.nodes["Page_0"]["abstract"] == "Page_0 abstract."
    assert store.graph.nodes["Page_3"]["abstract"] is None
    assert store.is_abstract_hydrated("Page_3")
    assert not store.is_abstract_hydrated("Category:Food")
    # saved to disk, so a restart keeps them
    saved = json.loads((tmp_path / "graph_cache.json").read_text(encoding="utf-8"))
    assert saved["nodes"]["Page_0"]["abstract"] == "Page_0 abstract."


async def test_hydrate_abstracts_saves_last_batches_despite_throttled_writes(tmp_path):
    # A long persist interval skips every unforced write; the last batch also ends exactly on
    # a save point (unsaved reset to 0). The end of the backfill must still force a write.
    from app.hydration import PERSIST_EVERY_BATCHES
    pages = [f"Page_{i}" for i in range(20 * PERSIST_EVERY_BATCHES)]
    store = seeded_store(tmp_path, pages)
    store.persist_interval_seconds = 3600
    async with store.write_lock():
        await store.persist(force=True)   # the throttle's clock starts now
    hydration = HydrationService(store, None, None, StubWikipedia())

    await hydration.hydrate_abstracts(pages)

    saved = json.loads((tmp_path / "graph_cache.json").read_text(encoding="utf-8"))
    assert saved["nodes"][pages[-1]]["abstract"] == f"{pages[-1]} abstract."


async def test_hydrate_abstracts_stops_at_the_first_failed_batch_and_a_later_call_resumes(tmp_path):
    pages = [f"Page_{i}" for i in range(65)]
    store = seeded_store(tmp_path, pages)
    wiki = StubWikipedia(fail_on={"Page_20"})
    hydration = HydrationService(store, None, None, wiki)

    report = await hydration.hydrate_abstracts(pages)
    # batch 1 done, batch 2 failed, batches 3-4 never sent: one outage costs one batch's retries
    assert len(wiki.batches) == 2
    assert (report.with_abstract, report.failed, report.remaining) == (20, 20, 25)
    assert report.stopped == "wikipedia_unavailable"
    assert store.is_abstract_hydrated("Page_0") and not store.is_abstract_hydrated("Page_20")
    assert not store.is_abstract_hydrated("Page_64")

    # Next run fetches only what is left.
    retry = StubWikipedia()
    hydration.wikipedia_client = retry
    report = await hydration.hydrate_abstracts(pages)
    assert retry.batches == [pages[20:40], pages[40:60], pages[60:]]
    assert (report.requested, report.failed, report.remaining, report.stopped) == (45, 0, 0, None)


async def test_hydrate_abstracts_fetches_at_most_limit_pages_per_call(tmp_path):
    pages = [f"Page_{i}" for i in range(45)]
    store = seeded_store(tmp_path, pages)
    wiki = StubWikipedia()
    hydration = HydrationService(store, None, None, wiki)

    first = await hydration.hydrate_abstracts(pages, limit=30)
    assert (first.requested, first.with_abstract, first.remaining) == (30, 30, 15)
    assert [len(b) for b in wiki.batches] == [20, 10]
    second = await hydration.hydrate_abstracts(pages, limit=30)
    assert (second.requested, second.remaining) == (15, 0)


async def test_hydrate_abstracts_refresh_refetches(tmp_path):
    store = seeded_store(tmp_path, ["A", "B"])
    wiki = StubWikipedia()
    hydration = HydrationService(store, None, None, wiki)
    await hydration.hydrate_abstracts(["A", "B"])
    await hydration.hydrate_abstracts(["A", "B"])
    assert len(wiki.batches) == 1  # already hydrated: no second call
    await hydration.hydrate_abstracts(["A", "B"], refresh=True)
    assert len(wiki.batches) == 2


async def test_hydrate_abstracts_skips_names_wikipedia_cannot_take(tmp_path):
    store = seeded_store(tmp_path, ["Good", "Bad|name"])
    wiki = StubWikipedia()
    report = await HydrationService(store, None, None, wiki).hydrate_abstracts(["Good", "Bad|name"])
    assert wiki.batches == [["Good"]]
    assert store.is_abstract_hydrated("Bad|name")
    assert store.graph.nodes["Bad|name"]["abstract"] is None
    assert (report.with_abstract, report.without_abstract) == (1, 1)


# ----------------------------------------------------------------------
# API
# ----------------------------------------------------------------------
class StubSparql:
    """Category:Food has three member pages; no synonyms beyond labels."""

    def __init__(self):
        self.calls = 0

    async def _count(self, value):
        self.calls += 1
        return value

    async def ask_category_exists(self, c, n): return await self._count(True)
    async def fetch_subcategories(self, c, n): return await self._count([])
    async def fetch_member_articles(self, c, n): return await self._count(["Citric_acid", "Sugar", "Mercury"])
    async def fetch_labels(self, c, n): return await self._count([])
    async def fetch_alt_names(self, c, n): return await self._count([])
    async def fetch_redirect_sources(self, c, n): return await self._count([])
    async def fetch_disambiguation_sources(self, c, n): return await self._count([])


@pytest.fixture
async def api(tmp_path):
    store = GraphStore(str(tmp_path / "graph_cache.json"))
    wiki = StubWikipedia(no_abstract={"Mercury"})
    http_client = httpx.AsyncClient()
    local_app = copy.copy(app)
    local_app.state = State()
    local_app.state.store = store
    local_app.state.hydration = HydrationService(store, StubSparql(), http_client, wiki)
    local_app.state.http_client = http_client
    async with httpx.AsyncClient(transport=ASGITransport(app=local_app), base_url="http://test") as client:
        yield client, store, wiki
    await http_client.aclose()


def nodes_by_id(body):
    return {n["id"]: n for n in body["nodes"]}


async def test_dig_fills_abstracts_and_graph_data_returns_them(api):
    client, _store, wiki = api
    resp = await client.post("/api/v1/dig", json={"categories": ["Category:Food"], "max_depth": 1})
    assert resp.status_code == 200
    assert len(wiki.batches) == 1

    nodes = nodes_by_id((await client.get("/api/v1/graph/data", params={"root": "Category:Food"})).json())
    assert nodes["Citric_acid"]["abstract"] == "Citric_acid abstract."
    assert nodes["Citric_acid"]["abstract_hydrated"] is True
    assert nodes["Citric_acid"]["abstract_fetched_at"]
    assert nodes["Mercury"]["abstract"] is None and nodes["Mercury"]["abstract_hydrated"] is True
    assert nodes["Category:Food"]["abstract"] is None and nodes["Category:Food"]["abstract_hydrated"] is False


async def test_hydrate_abstracts_backfills_cached_graph(api):
    client, store, wiki = api
    store.ensure_node("Category:Food", "rdfs:label")
    for page in ["Citric_acid", "Sugar"]:
        store.ensure_node(page, "rdfs:label")
        store.ensure_edge("Category:Food", page)

    resp = await client.post("/api/v1/hydrate-abstracts", json={"root": "Category:Food"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["root"] == "Category:Food"
    assert (body["requested"], body["with_abstract"], body["failed"], body["wikipedia_calls_used"]) == (2, 2, 0, 1)

    again = (await client.post("/api/v1/hydrate-abstracts", json={"root": "Category:Food"})).json()
    assert again["requested"] == 0 and again["wikipedia_calls_used"] == 0
    refreshed = (await client.post("/api/v1/hydrate-abstracts", json={"root": "Category:Food", "refresh": True})).json()
    assert refreshed["requested"] == 2


async def test_hydrate_abstracts_repeats_until_nothing_remains(api):
    client, store, wiki = api
    store.ensure_node("Category:Food", "rdfs:label")
    for i in range(25):
        store.ensure_node(f"Page_{i}", "rdfs:label")
        store.ensure_edge("Category:Food", f"Page_{i}")

    first = (await client.post("/api/v1/hydrate-abstracts", json={"root": "Category:Food", "limit": 20})).json()
    assert (first["limit"], first["requested"], first["remaining"]) == (20, 20, 5)
    second = (await client.post("/api/v1/hydrate-abstracts", json={"root": "Category:Food", "limit": 20})).json()
    assert (second["requested"], second["remaining"]) == (5, 0)


async def test_hydrate_abstracts_one_call_per_root_at_a_time(api):
    client, store, _wiki = api
    store.ensure_node("Category:Food", "rdfs:label")
    hydration = client._transport.app.state.hydration
    hydration.abstract_roots_running.add("Category:Food")       # a call for this root is in progress
    busy = await client.post("/api/v1/hydrate-abstracts", json={"root": "Category:Food"})
    assert busy.status_code == 409
    hydration.abstract_roots_running.discard("Category:Food")
    assert (await client.post("/api/v1/hydrate-abstracts", json={"root": "Category:Food"})).status_code == 200
    assert hydration.abstract_roots_running == set()           # released after the call


async def test_dig_fetches_at_most_abstracts_per_request(api, monkeypatch):
    import dataclasses

    from app.config import settings
    client, _store, wiki = api
    monkeypatch.setattr("app.dig.settings", dataclasses.replace(settings, abstracts_per_request=1))
    resp = await client.post("/api/v1/dig", json={"categories": ["Category:Food"], "max_depth": 1})
    assert resp.status_code == 200
    assert sum(len(b) for b in wiki.batches) == 1


async def test_hydrate_abstracts_unknown_root_is_404(api):
    client, _store, _wiki = api
    resp = await client.post("/api/v1/hydrate-abstracts", json={"root": "Category:Not_cached"})
    assert resp.status_code == 404
