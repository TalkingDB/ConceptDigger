"""
Comparison of actual vs. expected output and the final pass / fail verdict.

The verdict keeps four outcomes apart (this is what the HTML report shows):

    pass                    everything matched and every live query succeeded
    sparql_network_failure  endpoint unreachable / timed out / HTTP error / lookups skipped
    assertion_failure       the live data was fetched fine but differs from expected output
    execution_error         the test or harness itself broke

A live failure is never turned into a pass: if any SPARQL lookup failed during
a case, the case fails as `sparql_network_failure` even if the surviving data
happened to match.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tests.live_harness import (
    Case,
    CaseRun,
    ExpectedOutputMismatch,
    HarnessError,
    LiveSparqlFailure,
    ProbeResult,
    SparqlCall,
)

ALLOWED_SOURCES = {"rdfs:label", "alt_name", "redirect", "disambiguation"}
RECORD_KEYS = {"entity_url", "surface_text", "seed_category", "how_this_record"}
CATEGORY_PREFIX = "DBPedia>Category:"

PASS = "pass"
NETWORK = "sparql_network_failure"
ASSERTION = "assertion_failure"
EXECUTION = "execution_error"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Verdict:
    kind: str
    message: str = ""

    @property
    def passed(self) -> bool:
        return self.kind == PASS


def _rec_key(r: dict) -> tuple:
    return (r.get("entity_url"), r.get("surface_text"), r.get("how_this_record"))


def dig_checks(case: Case, expected: dict, run: CaseRun) -> list[Check]:
    """Compare one /dig response with the stored expected output."""
    checks: list[Check] = []
    want_status = expected.get("status_code", 200)
    checks.append(
        Check("HTTP status", run.status_code == want_status, f"expected {want_status}, actual {run.status_code}")
    )

    if want_status != 200:
        needle = expected.get("detail_contains")
        detail = run.body.get("detail") if isinstance(run.body, dict) else None
        if needle is not None:
            checks.append(
                Check(
                    f"error detail contains {needle!r}",
                    isinstance(detail, str) and needle in detail,
                    f"actual detail: {detail!r}",
                )
            )
        return checks

    body = run.body
    if not isinstance(body, list):
        checks.append(Check("response is a JSON array", False, f"actual: {type(body).__name__}"))
        return checks
    checks.append(Check("response is a JSON array", True, f"{len(body)} record(s)"))

    bad_shape = [r for r in body if not isinstance(r, dict) or set(r) != RECORD_KEYS]
    checks.append(Check("every record has exactly the 4 documented fields", not bad_shape,
                        f"{len(bad_shape)} malformed record(s)" if bad_shape else ""))
    records = [r for r in body if isinstance(r, dict)]

    bad = [r for r in records if not str(r.get("entity_url", "")).startswith("DBPedia>")]
    checks.append(Check("entity_url starts with 'DBPedia>'", not bad, f"{len(bad)} violation(s)" if bad else ""))

    bad = [r for r in records if r.get("how_this_record") not in ALLOWED_SOURCES]
    checks.append(Check("how_this_record is one of rdfs:label/alt_name/redirect/disambiguation", not bad,
                        f"unexpected: {sorted({str(r.get('how_this_record')) for r in bad})}" if bad else ""))

    bad = [r for r in records if r.get("surface_text") != str(r.get("surface_text", "")).lower()]
    checks.append(Check("surface_text is lower-case", not bad, f"{len(bad)} violation(s)" if bad else ""))

    seed = case.local_name
    what = "article" if case.kind == "article" else "dug category"
    bad = [r for r in records if r.get("seed_category") != seed]
    checks.append(Check(f"seed_category is the {what} ({seed})", not bad,
                        f"{len(bad)} record(s) attribute another seed" if bad else ""))

    entities = {r.get("entity_url", "") for r in records}
    if case.kind == "category" and not case.return_categories:
        bad = sorted(e for e in entities if e.startswith(CATEGORY_PREFIX))
        checks.append(Check("return_categories=false: no Category: entities", not bad, f"found: {bad[:5]}" if bad else ""))
    if case.kind == "category" and not case.return_pages:
        bad = sorted(e for e in entities if not e.startswith(CATEGORY_PREFIX))
        checks.append(Check("return_pages=false: only Category: entities", not bad, f"found: {bad[:5]}" if bad else ""))

    min_entities = int(expected.get("min_distinct_entities", 0))
    checks.append(Check(f"at least {min_entities} distinct entities", len(entities) >= min_entities,
                        f"actual {len(entities)}"))

    have = {_rec_key(r) for r in records}
    for want in expected.get("must_include_records", []):
        key = _rec_key(want)
        checks.append(Check(
            f"expected record present: {want['entity_url']} | {want['surface_text']} | {want['how_this_record']}",
            key in have,
            "" if key in have else "MISSING from actual output",
        ))
    for ent in expected.get("must_not_include_entities", []):
        present = f"DBPedia>{ent}" in entities or ent in entities
        checks.append(Check(f"forbidden entity absent: {ent}", not present, "PRESENT in actual output" if present else ""))
    return checks


def conclude(
    *,
    probe: ProbeResult | None,
    calls: list[SparqlCall],
    checks: list[Check],
    exception_text: str | None = None,
    network_exception: bool = False,
    status_code: int | None = None,
    require_live_queries: bool = True,
) -> Verdict:
    """Fold probe, recorded SPARQL calls, exception and checks into one verdict."""
    if probe is not None and not probe.ok:
        return Verdict(NETWORK, f"Endpoint probe failed for {probe.endpoint}: {probe.error}")
    if exception_text:
        last = exception_text.strip().splitlines()[-1]
        return Verdict(NETWORK if network_exception else EXECUTION,
                       f"Exception during run: {last}")
    if status_code == 502:
        return Verdict(NETWORK, "ConceptDigger answered HTTP 502: the SPARQL endpoint was unavailable after retries.")
    failed = [c for c in calls if c.error]
    if failed:
        first = failed[0]
        return Verdict(
            NETWORK,
            f"{len(failed)} of {len(calls)} live SPARQL lookup(s) failed after retries "
            f"(first: [{first.kind}] {first.error}). ConceptDigger skips such lookups, so the output may be "
            "incomplete; the case is failed instead of being compared.",
        )
    if require_live_queries and not calls:
        return Verdict(EXECUTION, "No SPARQL query was sent to the live endpoint - the test did not exercise live data.")
    bad = [c for c in checks if not c.ok]
    if bad:
        return Verdict(ASSERTION, "; ".join(f"{c.name} ({c.detail})" if c.detail else c.name for c in bad))
    return Verdict(PASS, "")


def raise_for_verdict(v: Verdict) -> None:
    if v.kind == PASS:
        return
    if v.kind == NETWORK:
        raise LiveSparqlFailure(v.message)
    if v.kind == ASSERTION:
        raise ExpectedOutputMismatch(v.message)
    raise HarnessError(v.message)
