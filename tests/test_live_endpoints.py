"""
Live tests for the other API behaviours that depend on real SPARQL answers:
the per-request call budget, category-only digging, and hydrate -> graph data.
Each test uses its own empty graph cache and the real endpoint.
"""
from dataclasses import replace
from unittest.mock import patch

import pytest

from app.config import settings
from tests.expectations import CATEGORY_PREFIX, Check, conclude, dig_checks, raise_for_verdict
from tests.live_harness import CaseRun, attempt, get_case, load_expected
from tests.reporting import LiveView, register_view

pytestmark = pytest.mark.live

# Input case (tests/debug_data) these endpoint tests reuse; the integrity test
# fails with a clear message if it is removed from the input data.
GRAPH_CASE_ID = "noble_gases"


def _view(request, title, inputs, api_call, req, expected, actual, verdict, checks, calls,
          status=None, headers=None, exc=None, duration=0.0):
    register_view(request.node.nodeid, LiveView(
        title=title, inputs=inputs, api_call=api_call, request=req, expected=expected, actual=actual,
        verdict=verdict, checks=checks, calls=calls, status_code=status, headers=headers or {},
        exception=exc, duration_s=duration))


async def test_live_budget_exhausted_header(live_stack, sparql_probe, request):
    """A budget of 1 live call is spent on the existence ASK; the response must say so."""
    sparql_probe.require()
    case = get_case(GRAPH_CASE_ID)
    tight = replace(settings, max_sparql_calls_per_request=1)
    body = {"categories": [case.url], "max_depth": 1}
    with patch("app.routers.dig.settings", tight), patch("app.dig.settings", tight):
        resp, exc, net = await attempt(live_stack.api.post("/api/v1/dig", json=body))
    checks = []
    if resp is not None:
        checks = [
            Check("HTTP status 200", resp.status_code == 200, f"actual {resp.status_code}"),
            Check("X-SPARQL-Budget-Exhausted header is 'true'",
                  resp.headers.get("x-sparql-budget-exhausted") == "true",
                  f"actual {resp.headers.get('x-sparql-budget-exhausted')!r}"),
            Check("body is an empty JSON array (no budget left to hydrate)", resp.json() == [],
                  f"actual {str(resp.json())[:120]}"),
        ]
    verdict = conclude(probe=sparql_probe, calls=live_stack.recorder.calls, checks=checks,
                       exception_text=exc, network_exception=net,
                       status_code=resp.status_code if resp is not None else None)
    _view(request, "Per-request SPARQL budget (MAX_SPARQL_CALLS_PER_REQUEST=1)",
          [("Wikipedia category URL", case.url), ("MAX_SPARQL_CALLS_PER_REQUEST (patched)", "1")],
          "POST /api/v1/dig", body,
          {"status_code": 200, "header X-SPARQL-Budget-Exhausted": "true", "body": []},
          resp.json() if resp is not None else None, verdict, checks, live_stack.recorder.calls,
          status=resp.status_code if resp is not None else None,
          headers={k: v for k, v in resp.headers.items() if k.startswith("x-sparql")} if resp is not None else {},
          exc=exc)
    raise_for_verdict(verdict)


async def test_live_dig_categories_only_returns_only_categories(live_stack, sparql_probe, request):
    """Same category as 'noble_gases' but return_pages=false (stored expectation: noble_gases_categories_only)."""
    sparql_probe.require()
    case = get_case("noble_gases_categories_only")
    expected = load_expected(case.id)
    resp, exc, net = await attempt(live_stack.api.post("/api/v1/dig", json=case.request_body))
    run = CaseRun(case=case, sparql_calls=list(live_stack.recorder.calls), exception=exc, network_exception=net)
    if resp is not None:
        run.status_code, run.body = resp.status_code, resp.json()
    checks = dig_checks(case, expected, run) if resp is not None else []
    verdict = conclude(probe=sparql_probe, calls=run.sparql_calls, checks=checks, exception_text=exc,
                       network_exception=net, status_code=run.status_code)
    records = run.body if isinstance(run.body, list) else None
    register_view(request.node.nodeid, LiveView(
        title="return_pages=false returns only sub-categories (own cache, own run)",
        inputs=[("Wikipedia category URL", case.url), ("return_categories / return_pages", "True / False")],
        api_call="POST /api/v1/dig", request=case.request_body, expected=expected, actual=run.body,
        actual_records=records, verdict=verdict, checks=checks, calls=run.sparql_calls,
        status_code=run.status_code, exception=exc))
    raise_for_verdict(verdict)


async def test_live_hydrate_then_graph_data(live_stack, sparql_probe, request):
    """/hydrate-category fills the cache from live data; /graph/data and /health then reflect exactly that."""
    sparql_probe.require()
    case = get_case(GRAPH_CASE_ID)
    expected = load_expected(case.id)
    root = case.local_name
    api = live_stack.api
    exc = net = None
    hyd = graph = health = None
    for name in ("hyd", "graph", "health"):
        if name == "hyd":
            call = api.post("/api/v1/hydrate-category", json={"category": case.url, "max_depth": 0})
        elif name == "graph":
            call = api.get("/api/v1/graph/data", params={"root": root, "depth": 1})
        else:
            call = api.get("/api/v1/health")
        resp, exc, net = await attempt(call)
        if exc:
            break
        if name == "hyd":
            hyd = resp
        elif name == "graph":
            graph = resp
        else:
            health = resp

    checks = []
    actual = {}
    if hyd is not None and graph is not None and health is not None:
        h, g, hl = hyd.json(), graph.json(), health.json()
        actual = {"hydrate-category": h, "graph/data": {"nodes": len(g["nodes"]), "edges": len(g["edges"]),
                  "sample_nodes": [n["id"] for n in g["nodes"][:15]]}, "health": hl}
        node_ids = {n["id"] for n in g["nodes"]}
        checks = [
            Check("hydrate-category HTTP 200", hyd.status_code == 200, f"actual {hyd.status_code}"),
            Check("hydrate-category echoes the normalised category", h.get("category") == root, f"actual {h.get('category')!r}"),
            Check("hydrate-category used exactly 2 live calls (sub-categories + member articles)",
                  h.get("sparql_calls_used") == 2, f"actual {h.get('sparql_calls_used')}"),
            Check("budget not exhausted", h.get("budget_exhausted") is False, f"actual {h.get('budget_exhausted')}"),
            Check("root node present in /graph/data", root in node_ids, ""),
            Check("every edge starts at the root (depth 1)", all(e["source"] == root for e in g["edges"]), ""),
            Check("new_nodes equals graph nodes (cache was empty)", h.get("new_nodes") == len(g["nodes"]),
                  f"new_nodes={h.get('new_nodes')} nodes={len(g['nodes'])}"),
            Check("new_edges equals graph edges", h.get("new_edges") == len(g["edges"]),
                  f"new_edges={h.get('new_edges')} edges={len(g['edges'])}"),
            Check("/health counts equal the cached graph",
                  (hl.get("graph_nodes"), hl.get("graph_edges")) == (len(g["nodes"]), len(g["edges"])),
                  f"health={hl}"),
        ]
        for want in expected.get("must_include_records", []):
            ent = want["entity_url"].removeprefix("DBPedia>")
            if ent.startswith("Category:"):
                continue
            checks.append(Check(f"expected member in graph: {ent}", ent in node_ids,
                                "" if ent in node_ids else "MISSING from live graph"))
    verdict = conclude(probe=sparql_probe, calls=live_stack.recorder.calls, checks=checks, exception_text=exc,
                       network_exception=bool(net))
    register_view(request.node.nodeid, LiveView(
        title="Hydrate a category from live SPARQL, then read it back via /graph/data and /health",
        inputs=[("Wikipedia category URL", case.url), ("Normalised DBpedia key", root)],
        api_call="POST /api/v1/hydrate-category -> GET /api/v1/graph/data?depth=1 -> GET /api/v1/health",
        request={"category": case.url, "max_depth": 0}, expected={"anchor_entities_from": f"expected_output/{case.id}.json",
                 "sparql_calls_used": 2, "health == graph": True}, actual=actual,
        verdict=verdict, checks=checks, calls=live_stack.recorder.calls, exception=exc))
    raise_for_verdict(verdict)
