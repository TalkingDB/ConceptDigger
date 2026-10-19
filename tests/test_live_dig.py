"""
Live integration tests for POST /api/v1/dig and article synonym hydration.

Flow per case (data in tests/debug_data):

    input Wikipedia category or article URL
      -> ConceptDigger (real app)
      -> live SPARQL queries to the configured endpoint
      -> actual output
      -> compare with debug_data/expected_output/<id>.json
      -> pass / fail (+ outcome type) -> HTML report

Category cases call POST /dig. Article cases call POST /hydrate-synonyms and
then read that article's label, alternate names, redirects and disambiguation
text back from the graph. Cases run in parallel once per session, with one
live SPARQL call in flight at a time. No mocked SPARQL data is involved.
"""
import pytest

from tests.expectations import conclude, dig_checks, raise_for_verdict
from tests.live_harness import get_case, load_cases, load_expected
from tests.reporting import LiveView, register_view

CASES = load_cases()

pytestmark = pytest.mark.live


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_dig_matches_expected_output(case, live_dig_results, sparql_probe, live_config, request):
    expected = load_expected(case.id)
    run = live_dig_results.runs[case.id]
    checks = dig_checks(case, expected, run) if run.status_code is not None else []
    verdict = conclude(
        probe=sparql_probe,
        calls=run.sparql_calls,
        checks=checks,
        exception_text=run.exception,
        network_exception=run.network_exception,
        status_code=run.status_code,
    )
    records = run.body if isinstance(run.body, list) and all(isinstance(r, dict) for r in run.body) else None
    register_view(
        request.node.nodeid,
        LiveView(
            title=f"{case.id}: {case.description}",
            inputs=[
                ("Test id", case.id),
                ("Input kind", case.kind),
                ("Wikipedia URL", case.url),
                ("Normalised DBpedia resource", f"http://dbpedia.org/resource/{case.local_name}"),
                ("max_depth / return_categories / return_pages",
                 f"{case.max_depth} / {case.return_categories} / {case.return_pages}"),
            ],
            api_call=(
                "POST /api/v1/hydrate-synonyms then GET /api/v1/graph/data?depth=0"
                if case.kind == "article"
                else "POST /api/v1/dig"
            ),
            request=case.request_body,
            expected=expected,
            actual=run.body if run.body is not None else (run.raw_text or None),
            actual_records=records,
            status_code=run.status_code,
            headers=run.headers,
            verdict=verdict,
            checks=checks,
            calls=run.sparql_calls,
            duration_s=run.duration_s,
            exception=run.exception,
            notes=[f"started {run.offset_s:.1f}s after the batch began (parallel cases, one SPARQL call at a time)",
                   f"{len(run.sparql_calls)} live SPARQL queries"],
        ),
    )
    raise_for_verdict(verdict)


def test_live_dig_batch_within_time_budget(live_dig_results, sparql_probe, live_config, request):
    """The whole live batch must finish inside the configured budget (default 60 s)."""
    from tests.expectations import Check, Verdict, ASSERTION, PASS
    sparql_probe.require()
    ok = live_dig_results.wall_seconds <= live_config.time_budget
    check = Check("live dig wall time within budget", ok,
                  f"{live_dig_results.wall_seconds:.1f}s used of {live_config.time_budget:.0f}s")
    verdict = Verdict(PASS if ok else ASSERTION, "" if ok else check.detail + " - exceeded the time budget")
    register_view(
        request.node.nodeid,
        LiveView(
            title="Time budget for the live digs",
            inputs=[("Wikipedia category URL", f"{len(live_dig_results.runs)} categories dug in parallel"),
                    ("Budget (TEST_SUITE_TIME_BUDGET_SECONDS)", f"{live_config.time_budget:.0f}s")],
            api_call="(timing of the batch above)", request={}, expected={"max_wall_seconds": live_config.time_budget},
            actual={"wall_seconds": round(live_dig_results.wall_seconds, 2)},
            verdict=verdict, checks=[check], calls=[],
        ),
    )
    if not ok:
        raise AssertionError(verdict.message)
