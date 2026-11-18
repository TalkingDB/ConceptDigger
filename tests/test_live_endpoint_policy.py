"""
Offline checks of how the live harness treats a misbehaving SPARQL endpoint:
a skip by default (never a pass, never a failure blamed on ConceptDigger), a
failure with TEST_LIVE_STRICT=1, and a real output mismatch always a failure.
"""
from types import SimpleNamespace

import pytest

from tests.expectations import ASSERTION, NETWORK, Verdict, raise_for_verdict
from tests.live_harness import (
    Case,
    CaseRun,
    ExpectedOutputMismatch,
    LiveSparqlFailure,
    LiveSparqlSkip,
    ProbeResult,
    SparqlCall,
    had_endpoint_trouble,
)
from tests.reporting import classify


@pytest.fixture
def lenient(monkeypatch):
    monkeypatch.delenv("TEST_LIVE_STRICT", raising=False)


@pytest.fixture
def strict(monkeypatch):
    monkeypatch.setenv("TEST_LIVE_STRICT", "1")


def _case() -> Case:
    return Case(id="c", url="https://en.wikipedia.org/wiki/Category:X", max_depth=0,
                return_categories=True, return_pages=True)


def _call(error=None) -> SparqlCall:
    return SparqlCall(kind="labels", query="", offset_s=0.0, error=error)


def test_network_verdict_is_a_skip_by_default(lenient):
    with pytest.raises(LiveSparqlSkip, match="2 of 99"):
        raise_for_verdict(Verdict(NETWORK, "2 of 99 live SPARQL lookup(s) failed after retries"))


def test_network_verdict_fails_when_strict(strict):
    with pytest.raises(LiveSparqlFailure):
        raise_for_verdict(Verdict(NETWORK, "endpoint down"))


def test_output_mismatch_always_fails(lenient):
    with pytest.raises(ExpectedOutputMismatch):
        raise_for_verdict(Verdict(ASSERTION, "missing node Argon"))


def test_unreachable_endpoint_is_a_skip_by_default(lenient):
    with pytest.raises(LiveSparqlSkip, match="not reachable"):
        ProbeResult(ok=False, endpoint="https://dbpedia.org/sparql", error="ConnectTimeout").require()


def test_unreachable_endpoint_fails_when_strict(strict):
    with pytest.raises(LiveSparqlFailure):
        ProbeResult(ok=False, endpoint="https://dbpedia.org/sparql", error="ConnectTimeout").require()


def test_endpoint_trouble_is_detected_per_run():
    assert not had_endpoint_trouble(CaseRun(case=_case(), status_code=200, sparql_calls=[_call()]))
    assert had_endpoint_trouble(CaseRun(case=_case(), sparql_calls=[_call(), _call("timed out")]))
    assert had_endpoint_trouble(CaseRun(case=_case(), status_code=502))
    assert had_endpoint_trouble(CaseRun(case=_case(), network_exception=True))


def test_report_counts_an_endpoint_skip_as_network(lenient):
    try:
        raise_for_verdict(Verdict(NETWORK, "endpoint down"))
    except LiveSparqlSkip as exc:
        call = SimpleNamespace(excinfo=SimpleNamespace(value=exc))
    skipped = SimpleNamespace(skipped=True, passed=False)
    assert classify(skipped, call) == NETWORK
    plain_skip = SimpleNamespace(excinfo=SimpleNamespace(value=pytest.skip.Exception("other")))
    assert classify(skipped, plain_skip) == "skipped"
