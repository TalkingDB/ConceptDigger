"""
HTML / JSON reporting for the live test suite.

This extends the *existing* pytest-html report (same file, same pytest.ini
options) instead of replacing it:

  * a run-overview block and a "How to Reproduce These Tests" section are added
    around the standard summary,
  * two columns are added to the results table: outcome type and input URL,
  * every live test gets an expandable detail row (input, SPARQL calls,
    checks, expected vs. actual output, execution details).

Only the CURRENT run is kept: stale report artifacts and the old `history/`
folder are deleted when a run starts.
"""
from __future__ import annotations

import html
import json
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pytest_html import extras

from app.sparql_client import _PREFIXES
from tests.expectations import ASSERTION, EXECUTION, NETWORK, PASS, Check, Verdict
from tests.live_harness import (
    EXPECTED_DIR,
    INPUT_FILE,
    LiveSparqlFailure,
    ProbeResult,
    SparqlCall,
    load_cases,
)

OUTCOME_LABELS = {
    PASS: ("PASS", "#1a7f37"),
    ASSERTION: ("ASSERTION FAILURE", "#cf222e"),
    NETWORK: ("SPARQL / NETWORK FAILURE", "#bc4c00"),
    EXECUTION: ("TEST EXECUTION ERROR", "#8250df"),
    "skipped": ("SKIPPED", "#6e7781"),
}

MAX_TABLE_ROWS = 300
MAX_JSON_CHARS = 60_000

# Filled in by conftest fixtures while the run is in progress.
STATE = SimpleNamespace(cfg=None, probe=None, batch=None, cases=None, started_utc=None)


@dataclass
class LiveView:
    title: str
    inputs: list[tuple[str, str]]
    api_call: str
    request: Any
    expected: Any
    actual: Any
    verdict: Verdict
    checks: list[Check]
    calls: list[SparqlCall]
    status_code: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    actual_records: list[dict] | None = None
    duration_s: float = 0.0
    exception: str | None = None
    notes: list[str] = field(default_factory=list)


VIEWS: dict[str, LiveView] = {}


def register_view(nodeid: str, view: LiveView) -> None:
    VIEWS[nodeid] = view


# ----------------------------------------------------------------------
# HTML helpers
# ----------------------------------------------------------------------
def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def badge(kind: str) -> str:
    label, color = OUTCOME_LABELS.get(kind, (kind, "#6e7781"))
    return (f'<span style="background:{color};color:#fff;padding:2px 8px;border-radius:10px;'
            f'font-size:12px;font-weight:600;white-space:nowrap">{esc(label)}</span>')


def pre_json(obj: Any) -> str:
    text = json.dumps(obj, indent=2, ensure_ascii=False, default=str)
    if len(text) > MAX_JSON_CHARS:
        text = text[:MAX_JSON_CHARS] + "\n... (truncated in report)"
    return f'<pre class="cd-pre">{esc(text)}</pre>'


def _table(headers: list[str], rows: list[list[str]]) -> str:
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<table class="cd-table"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def _calls_html(calls: list[SparqlCall]) -> str:
    if not calls:
        return "<p><em>No SPARQL query was recorded for this test.</em></p>"
    total = sum(c.duration_s for c in calls)
    failed = sum(1 for c in calls if c.error)
    rows = []
    for i, c in enumerate(calls, 1):
        status = ",".join(str(s) for s in c.http_statuses) or "-"
        outcome = esc(c.result) if not c.error else f'<b style="color:#bc4c00">FAILED: {esc(c.error)}</b>'
        rows.append([
            str(i), esc(c.kind), f"{c.duration_s:.2f}s", esc(status), outcome,
            f'<details><summary>show</summary><pre class="cd-pre">{esc(c.query)}</pre></details>',
        ])
        if i >= 80:
            rows.append(["…", f"{len(calls) - 80} more query(ies) not listed", "", "", "", ""])
            break
    summary = (f"<p>{len(calls)} live SPARQL quer{'y' if len(calls) == 1 else 'ies'} sent, "
               f"{failed} failed, {total:.1f}s cumulative query time.</p>")
    return summary + _table(["#", "Purpose", "Time", "HTTP status(es)", "Result", "Query"], rows)


def _actual_table(records: list[dict], expected_keys: set[tuple]) -> str:
    rows = []
    for r in records[:MAX_TABLE_ROWS]:
        key = (r.get("entity_url"), r.get("surface_text"), r.get("how_this_record"))
        mark = "✔ expected" if key in expected_keys else ""
        rows.append([esc(r.get("entity_url", "")), esc(r.get("surface_text", "")),
                     esc(r.get("how_this_record", "")), esc(r.get("seed_category", "")), mark])
    note = ""
    if len(records) > MAX_TABLE_ROWS:
        note = f"<p><em>Showing first {MAX_TABLE_ROWS} of {len(records)} records.</em></p>"
    return note + _table(["entity_url", "surface_text", "how_this_record", "seed_category", "matches expected"], rows)


def render_view(v: LiveView) -> str:
    parts = ['<div class="cd-detail">']
    parts.append(f"<h4>{esc(v.title)} &nbsp; {badge(v.verdict.kind)}</h4>")
    if v.verdict.message:
        parts.append(f'<p class="cd-why"><b>Why:</b> {esc(v.verdict.message)}</p>')

    parts.append("<h5>Input</h5>")
    parts.append(_table(["Field", "Value"], [[esc(k), esc(val)] for k, val in v.inputs]))

    parts.append("<h5>SPARQL / API information</h5>")
    parts.append(f"<p>API call (in-process ASGI app): <code>{esc(v.api_call)}</code><br>"
                 f"Request body / params:</p>{pre_json(v.request)}")
    if STATE.cfg:
        parts.append(f"<p>SPARQL endpoint queried: <code>{esc(STATE.cfg.endpoint)}</code></p>")
    parts.append(_calls_html(v.calls))

    parts.append("<h5>Checks (expected vs. actual)</h5>")
    rows = [[("✔" if c.ok else '<b style="color:#cf222e">✘</b>'), esc(c.name), esc(c.detail)] for c in v.checks]
    parts.append(_table(["", "Check", "Detail"], rows) if rows else "<p><em>No checks were evaluated.</em></p>")

    parts.append("<h5>Expected output (stored test data)</h5>")
    parts.append(pre_json(v.expected))

    parts.append("<h5>Actual output (live)</h5>")
    meta = []
    if v.status_code is not None:
        meta.append(f"HTTP status: {esc(v.status_code)}")
    if v.headers:
        meta.append("headers: " + esc(json.dumps(v.headers)))
    if meta:
        parts.append("<p>" + " &nbsp; ".join(meta) + "</p>")
    if v.actual_records is not None:
        exp_keys = {(r["entity_url"], r["surface_text"], r["how_this_record"])
                    for r in (v.expected or {}).get("must_include_records", [])} if isinstance(v.expected, dict) else set()
        parts.append(_actual_table(v.actual_records, exp_keys))
    parts.append(f"<details><summary>raw actual JSON</summary>{pre_json(v.actual)}</details>")

    parts.append("<h5>Execution details</h5>")
    details = [f"duration: {v.duration_s:.2f}s", f"python: {sys.version.split()[0]}"] + v.notes
    parts.append("<ul>" + "".join(f"<li>{esc(d)}</li>" for d in details) + "</ul>")
    if v.exception:
        parts.append(f'<details open><summary>exception</summary><pre class="cd-pre">{esc(v.exception)}</pre></details>')
    parts.append("</div>")
    return "".join(parts)


STYLE = """<style>
div.cd-box,div.cd-box p,div.cd-box td,div.cd-box th,div.cd-box h3,div.cd-box h4,div.cd-box li,div.cd-box summary,div.cd-detail,div.cd-detail p,div.cd-detail td,div.cd-detail th,div.cd-detail h4,div.cd-detail h5,div.cd-detail li,div.cd-detail summary{color:#1f2328}
div.cd-detail p.cd-why{color:#cf222e}
.cd-box{border:1px solid #d0d7de;border-radius:6px;padding:10px 16px;margin:12px 0;background:#f6f8fa}
.cd-table{border-collapse:collapse;margin:6px 0;font-size:12px}
.cd-table th,.cd-table td{border:1px solid #d0d7de;padding:3px 8px;text-align:left;vertical-align:top}
.cd-pre{background:#fff;border:1px solid #d0d7de;padding:8px;overflow:auto;max-height:340px;font-size:12px}
.cd-detail h5{margin:14px 0 4px}.cd-why{color:#cf222e}
.cd-box code{background:#eaeef2;padding:1px 4px;border-radius:3px}
</style>"""


# ----------------------------------------------------------------------
# run overview + reproduction section
# ----------------------------------------------------------------------
def _overview_html(plugin: "LiveReporting") -> str:
    cfg, probe, batch = STATE.cfg, STATE.probe, STATE.batch
    counts: dict[str, int] = {}
    for r in plugin.results:
        counts[r["outcome_type"]] = counts.get(r["outcome_type"], 0) + 1
    rows = []
    for kind in (PASS, ASSERTION, NETWORK, EXECUTION, "skipped"):
        rows.append([badge(kind), str(counts.get(kind, 0))])
    out = [STYLE, '<div class="cd-box"><h3>Run overview</h3>']
    info = [("Started (UTC)", STATE.started_utc or "-"),
            ("Total session time", f"{time.monotonic() - plugin.t0:.1f}s")]
    if cfg:
        info.append(("SPARQL endpoint under test", cfg.endpoint))
    if probe:
        status = "reachable" if probe.ok else f"UNREACHABLE: {probe.error}"
        info.append(("Endpoint probe (ASK { ?s ?p ?o })", f"{status} ({probe.duration_s:.2f}s)"))
    if batch and cfg:
        over = batch.wall_seconds > cfg.time_budget
        info.append(("Live dig wall time (all categories, parallel)",
                     f"{batch.wall_seconds:.1f}s of {cfg.time_budget:.0f}s budget"
                     + (" - OVER BUDGET" if over else " - within budget")))
    total_calls = sum(len(r.sparql_calls) for r in batch.runs.values()) if batch else 0
    info.append(("Live SPARQL queries in the dig batch", str(total_calls)))
    info.append(("Python", sys.version.split()[0]))
    out.append(_table(["Item", "Value"], [[esc(k), esc(v)] for k, v in info]))
    out.append("<p><b>Results by outcome type</b></p>" + _table(["Outcome type", "Tests"], rows))
    out.append("<p>Outcome types: <b>PASS</b> - all checks matched and every live query succeeded. "
               "<b>ASSERTION FAILURE</b> - live data was fetched but differs from the stored expected output. "
               "<b>SPARQL / NETWORK FAILURE</b> - the endpoint was unreachable, timed out, returned an error, "
               "or lookups were skipped after retries (never counted as a pass). "
               "<b>TEST EXECUTION ERROR</b> - the test or harness itself broke.</p></div>")
    return "".join(out)


def _case_what(case) -> str:
    if case.kind == "article":
        return "article: label, alt name, redirect, disambiguation"
    parts = []
    if case.return_categories:
        parts.append("categories")
    if case.return_pages:
        parts.append("pages")
    what = " and ".join(parts) or "nothing returned"
    return f"dig depth {case.max_depth}, {what}"


def _repro_html() -> str:
    cfg = STATE.cfg
    cases = STATE.cases or []
    batch = STATE.batch
    endpoint = cfg.endpoint if cfg else "https://dbpedia.org/sparql"
    url_rows = [
        [esc(c.id), esc(c.kind), f'<code>{esc(c.url)}</code>', esc(_case_what(c))]
        for c in cases
    ]

    samples: dict[str, SparqlCall] = {}
    if batch:
        for run in batch.runs.values():
            for call in run.sparql_calls:
                samples.setdefault(call.kind, call)
    query_html = ""
    if samples:
        blocks = [f"<p><b>{esc(kind)}</b></p><pre class=\"cd-pre\">{esc(call.query)}</pre>"
                  for kind, call in samples.items()]
        query_html = (
            "<h4>SPARQL queries used in this run</h4>"
            "<p>One sample per kind. Each query is prefixed with:</p>"
            f'<pre class="cd-pre">{esc(_PREFIXES.strip())}</pre>'
            + "".join(blocks)
        )

    cases_html = (
        _table(["id", "kind", "URL", "what it checks"], url_rows)
        if url_rows
        else "<p>No cases in <code>tests/debug_data/input_data/wikipedia_categories.json</code>.</p>"
    )
    return f"""<div class="cd-box"><h3>How to Reproduce These Tests</h3>
<ol>
<li><b>Run the app</b> from the repo root (Docker, port 5007):
<pre class="cd-pre">./start.sh</pre>
Same thing on Git Bash / WSL: <code>bash start.sh</code>. Health check:
<code>curl http://localhost:5007/api/v1/health</code></li>
<li><b>Run the tests</b> from the same checkout. They import the <code>app</code> package
(they do not call the Docker port) and query live DBpedia at <code>{esc(endpoint)}</code>.
<pre class="cd-pre">pip install -r requirements-dev.txt
pytest tests/</pre>
One case: <code>pytest tests/ -k helium</code>. Offline only: <code>pytest tests/ -m "not live"</code>.
The report is written to <code>tests/records/report.html</code>.</li>
<li><b>Cases in this run</b> ({len(cases)}). Input is
<code>tests/debug_data/input_data/wikipedia_categories.json</code>; expected output is
<code>tests/debug_data/expected_output/&lt;id&gt;.json</code>.
Category cases call <code>POST /api/v1/dig</code>. Article cases call
<code>POST /api/v1/hydrate-synonyms</code> and check label, alternate name, redirect and
disambiguation text.
{cases_html}</li>
</ol>
{query_html}
</div>"""


# ----------------------------------------------------------------------
# cleanup of old artifacts
# ----------------------------------------------------------------------
def cleanup_records(records_dir: Path) -> None:
    """Delete everything a previous run generated; keep README.md and .gitkeep."""
    records_dir.mkdir(parents=True, exist_ok=True)
    for name in ("report.html", "junit.xml", "summary.json"):
        (records_dir / name).unlink(missing_ok=True)
    for old in records_dir.glob("summary-*.json"):
        old.unlink(missing_ok=True)
    for folder in ("history", "assets"):
        shutil.rmtree(records_dir / folder, ignore_errors=True)


def classify(report, call) -> str:
    if report.skipped:
        return "skipped"
    if report.passed:
        return PASS
    exc = call.excinfo.value if call.excinfo else None
    if isinstance(exc, LiveSparqlFailure):
        return NETWORK
    if isinstance(exc, AssertionError):
        return ASSERTION
    return EXECUTION


class LiveReporting:
    """pytest plugin: classification, pytest-html extensions, summary.json."""

    def __init__(self, records_dir: Path):
        self.records_dir = records_dir
        self.results: list[dict] = []
        self.t0 = time.monotonic()

    # -- lifecycle -----------------------------------------------------
    def pytest_sessionstart(self, session):
        self.t0 = time.monotonic()
        STATE.started_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
        VIEWS.clear()
        cleanup_records(self.records_dir)

    def pytest_sessionfinish(self, session, exitstatus):
        counts: dict[str, int] = {}
        for r in self.results:
            counts[r["outcome_type"]] = counts.get(r["outcome_type"], 0) + 1
        cfg, batch = STATE.cfg, STATE.batch
        summary = {
            "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "exitstatus": int(exitstatus),
            "python": sys.version,
            "sparql_endpoint": cfg.endpoint if cfg else None,
            "session_seconds": round(time.monotonic() - self.t0, 2),
            "live_dig_wall_seconds": round(batch.wall_seconds, 2) if batch else None,
            "counts_by_outcome_type": counts,
            "tests": self.results,
        }
        self.records_dir.mkdir(parents=True, exist_ok=True)
        (self.records_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    # -- classification + detail rows ---------------------------------
    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item, call):
        outcome = yield
        report = outcome.get_result()
        if report.when == "teardown" or (report.when == "setup" and report.passed):
            return
        kind = classify(report, call)
        report.outcome_kind = kind
        view = VIEWS.get(report.nodeid)
        report.cd_input = next((v for k, v in view.inputs if k == "Wikipedia category URL"), "") if view else ""
        if view:
            report.extras = list(getattr(report, "extras", [])) + [extras.html(render_view(view))]
        self.results.append({
            "nodeid": report.nodeid,
            "outcome": report.outcome,
            "outcome_type": kind,
            "duration": report.duration,
            "message": (view.verdict.message if view and view.verdict.message
                        else (str(call.excinfo.value)[:500] if call.excinfo else None)),
        })

    # -- pytest-html hooks --------------------------------------------
    def pytest_html_report_title(self, report):
        report.title = "ConceptDigger - live Wikipedia / DBpedia SPARQL test report"

    def pytest_html_results_summary(self, prefix, summary, postfix, session):
        prefix.append(_overview_html(self))
        postfix.append(_repro_html())

    def pytest_html_results_table_header(self, cells):
        cells.insert(2, "<th>Outcome type</th>")
        cells.insert(3, "<th>Input Wikipedia category</th>")

    def pytest_html_results_table_row(self, report, cells):
        kind = getattr(report, "outcome_kind", PASS if report.passed else "")
        cells.insert(2, f"<td>{badge(kind) if kind else ''}</td>")
        cells.insert(3, f"<td>{esc(getattr(report, 'cd_input', ''))}</td>")
