# Test records

This directory stores **run artifacts** produced by `pytest`. Golden expected
responses live in `tests/test_data/golden/` and are committed.

Each local or CI run writes:

| File | Contents |
|---|---|
| `junit.xml` | JUnit report (GitHub Actions / other CI) |
| `report.html` | Self-contained HTML report (`pytest-html`) |
| `summary.json` | Latest JSON summary of every test outcome |
| `history/summary-*.json` | Timestamped copies of `summary.json` |

These generated files are gitignored. On GitHub Actions they are uploaded as
the `test-records` artifact.
