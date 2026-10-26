from app.graph_store import GraphStore
from tests.test_helpers import Stub, dig

async def test_structure_query_failure_returns_incomplete_status(tmp_path):
    stub = Stub(fail={"subcategories"})
    response = await dig(tmp_path, stub)
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "incomplete"
    assert body["failed_requests"] == 1
    assert isinstance(body["error"], str)
    assert "1 SPARQL requests failed" in body["error"]

    # Confirm that the failing query was attempted.
    assert any(call[0] == "subcategories" for call in stub.calls)


async def test_cached_and_failing_seed_returns_incomplete(tmp_path):
    """A cached seed and a failing seed in the same request return 200 with 'incomplete'."""
    store = GraphStore(str(tmp_path / "g.json"))
    
    # Warm up cache with a successful request for Category:Cached
    stub_success = Stub(articles=("Cached_Article",))
    await dig(tmp_path, stub_success, categories=["Category:Cached"], store=store)

    # Make a second request with both the cached category and a failing category
    stub_fail = Stub(fail={"subcategories"}, fail_categories={"Category:Failing"})
    response = await dig(
        tmp_path,
        stub_fail,
        categories=["Category:Cached", "Category:Failing"],
        store=store,
    )
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "incomplete"
    assert body["failed_requests"] >= 1
    assert len(body["data"]) > 0


async def test_synonym_lookup_failure_returns_incomplete(tmp_path):
    """Failures occurring only during synonym lookups (labels, redirects, etc.)."""
    stub = Stub(fail={"labels", "redirects", "alt_names", "disambiguations"})
    response = await dig(tmp_path, stub)
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "incomplete"
    assert body["failed_requests"] > 0
    assert "SPARQL requests failed" in body["error"]


async def test_total_failure_returns_failed_status(tmp_path):
    """Test the 'failed' status directly (returning 502 when all requests fail and no data is present)."""
    # Fail 'ask' for all categories so that no data is found at all
    stub = Stub(fail={"ask"}, ask_exists=False)
    response = await dig(tmp_path, stub)
    body = response.json()

    assert response.status_code == 502
    assert body["status"] == "failed"
    assert body["data"] == []
    assert isinstance(body["error"], str)