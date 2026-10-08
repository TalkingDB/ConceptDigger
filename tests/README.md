# ConceptDigger tests

The main tests are **live integration tests**: they take real English-Wikipedia
category and article URLs, run them through the real ConceptDigger app, which
queries a **live DBpedia SPARQL endpoint**, and compare the actual output with
expected output stored as test data. No mocked SPARQL responses are used.

```
Input Wikipedia category or article URL  (debug_data/input_data/wikipedia_categories.json)
        -> ConceptDigger (real app, in-process)
        -> live SPARQL queries to $SPARQL_ENDPOINT
        -> actual output
        -> compare with debug_data/expected_output/<id>.json
        -> PASS / FAIL (+ outcome type)
        -> tests/records/report.html
```

## Run

From the repository root (Python 3.12):

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
pytest tests/                                     # everything (needs internet access)
pytest tests/ -m "not live"                       # offline tests only
pytest tests/ -k helium                        # one case
```

`pytest.ini` (project root, unchanged) writes `tests/records/report.html`,
`junit.xml` and `summary.json`. Without that ini file add
`--html=tests/records/report.html --self-contained-html --junitxml=tests/records/junit.xml`.

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `SPARQL_ENDPOINT` | `https://dbpedia.org/sparql` | Endpoint under test (same variable the app reads) |
| `TEST_SPARQL_MIN_INTERVAL_SECONDS` | `0.1` | Aggregate gap between outgoing queries across all parallel cases |
| `TEST_SPARQL_TIMEOUT_SECONDS` | app setting (20) | Read timeout per query |
| `TEST_CONCURRENCY` | `5` | Categories dug in parallel |
| `TEST_SUITE_TIME_BUDGET_SECONDS` | `600` | Wall-time budget for the live digs (`test_live_dig_batch_within_time_budget`) |
| `SPARQL_MAX_RETRIES` | `3` | App setting: retries on 429/502/503/504 |

## Test data (`debug_data/`)

```
debug_data/
  input_data/wikipedia_categories.json   cases: id, kind (category or article), Wikipedia URL, and for categories max_depth, return_categories, return_pages
  expected_output/<id>.json              expected result for the case with that id
```

**Add a case:** append it to `wikipedia_categories.json` and create
`expected_output/<id>.json`. **Remove one:** delete both. `test_debug_data_integrity.py`
fails with a clear message if an input has no expected file (or vice versa).

`kind: category` digs a Wikipedia category (`POST /dig`). Keep those small
(`max_depth` 0): every collected node costs 4 live SPARQL queries.
`kind: article` sends one Wikipedia article URL (`POST /hydrate-synonyms`) and
checks that article's label, alternate names, redirects and disambiguation
text. An article case is 4 live queries.

### What "expected output" contains

Live DBpedia changes over time, so the expected files are *anchors*, not a full
snapshot: records that must be present (`must_include_records`), entities that
must be absent (`must_not_include_entities`), `min_distinct_entities`, and for
negative cases `status_code` + `detail_contains`. Every response is additionally
checked against rules of intended behaviour that need no stored data: documented
record shape, lower-case `surface_text`, `seed_category` equals the dug category,
and the `return_categories` / `return_pages` flags.

## Outcome types in the report

| Outcome type | Meaning |
|---|---|
| PASS | All checks matched and every live SPARQL query succeeded |
| ASSERTION FAILURE | Live data was fetched but differs from the expected output |
| SPARQL / NETWORK FAILURE | Endpoint unreachable, timeout, HTTP error, or lookups skipped after retries (ConceptDigger skips failed lookups; the test fails instead of comparing partial data). Never a pass |
| TEST EXECUTION ERROR | Bad data file, unexpected exception in the app/harness, or no live query was sent |

## Files

| File | Purpose |
|---|---|
| `test_live_dig.py` | One live test per input case + the time-budget test |
| `test_live_endpoints.py` | Live: call budget header, categories-only digging, hydrate -> graph data -> health |
| `test_api_validation.py` | Offline: request validation (SPARQL access fails the test) |
| `test_category.py`, `test_sparql_resilience.py` | Offline unit tests (URL parsing, retry logic) |
| `test_debug_data_integrity.py` | Offline: input/expected data consistency |
| `live_harness.py` | Live stack, recording SPARQL client, probe, case runner |
| `expectations.py` | Comparison checks and the four-way verdict |
| `reporting.py` | pytest-html extensions, `summary.json`, cleanup of old reports |
| `records/` | Output of the **latest run only** (git-ignored) |

## Note for the root README

The "Tests" section of the root `README.md` still says tests never call live
DBpedia. Suggested replacement: *"Integration tests query the live DBpedia SPARQL
endpoint using the Wikipedia category URLs in `tests/debug_data/`; see
`tests/README.md`. Run artifacts for the latest run are in `tests/records/`."*
