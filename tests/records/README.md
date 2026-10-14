# Test records

Output of the **most recent** `pytest` run only. Nothing from earlier runs is
kept: at the start of every run the previous `report.html`, `junit.xml`,
`summary.json` and any legacy `history/` folder are deleted.

| File | Contents |
|---|---|
| `report.html` | Self-contained HTML report (`pytest-html`): run overview, outcome types, per-test input / SPARQL queries / expected vs. actual, and "How to Reproduce These Tests" |
| `junit.xml` | JUnit report for CI |
| `summary.json` | JSON summary of every test with its outcome type |

Generated files are git-ignored. Expected data lives in `tests/debug_data/`.
