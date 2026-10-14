"""Offline consistency checks for tests/debug_data so a bad edit fails clearly (not mysteriously live)."""
import re

import pytest

from app.config import settings
from tests.live_harness import EXPECTED_DIR, HarnessError, load_cases, load_expected
from tests.test_live_endpoints import GRAPH_CASE_ID

CASES = load_cases()
CATEGORY_URL = re.compile(r"^https://en\.wikipedia\.org/wiki/Category:\S+$")
ARTICLE_URL = re.compile(r"^https://en\.wikipedia\.org/wiki/(?!Category:)\S+$")


def test_input_ids_are_unique_and_filename_safe():
    ids = [c.id for c in CASES]
    assert len(ids) == len(set(ids)), "duplicate case ids in input_data"
    assert all(re.fullmatch(r"[a-z0-9_]+", i) for i in ids), "ids must match [a-z0-9_]+"


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_case_is_well_formed_and_has_expected_output(case):
    if case.kind == "article":
        assert ARTICLE_URL.match(case.url), f"{case.url} is not an https://en.wikipedia.org/wiki/<Article> URL"
    else:
        assert case.kind == "category"
        assert CATEGORY_URL.match(case.url), f"{case.url} is not an https://en.wikipedia.org/wiki/Category:... URL"
    assert 0 <= case.max_depth <= settings.max_depth_hard_cap
    expected = load_expected(case.id)  # raises HarnessError if missing / invalid JSON
    assert expected.get("status_code", 200) in (200, 404)
    if expected.get("status_code", 200) == 200:
        for rec in expected.get("must_include_records", []):
            assert set(rec) == {"entity_url", "surface_text", "how_this_record"}


def test_no_orphan_expected_output_files():
    ids = {c.id for c in CASES}
    orphans = sorted(p.stem for p in EXPECTED_DIR.glob("*.json") if p.stem not in ids)
    assert not orphans, f"expected_output files without an input case: {orphans}"


def test_endpoint_tests_have_their_input_cases():
    ids = {c.id for c in CASES}
    for needed in (GRAPH_CASE_ID, "noble_gases_categories_only"):
        assert needed in ids, f"input case '{needed}' is required by tests/test_live_endpoints.py"
