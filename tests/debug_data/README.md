# debug_data

Live-test data for ConceptDigger. See `tests/README.md` for the full description.

- `input_data/wikipedia_categories.json` - Wikipedia category and article URLs, with dig parameters for categories.
- `expected_output/<id>.json` - stored expectation for the input case with the same `id`.

Category cases anchor member articles. Article cases anchor that article's
label, alternate names, redirects and disambiguation text. Expected files hold
anchors (must-include records, forbidden entities, minimum sizes), not a
snapshot of live data. Edit them by hand when DBpedia changes legitimately or
when you add a case.
