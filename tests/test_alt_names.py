"""
Offline checks for the fetch_alt_names query: every alternate-name property
is requested, only English/untagged literals are kept, and the returned
bindings become plain name strings.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.sparql_client import SparqlClient

ALT_NAME_PROPERTIES = [
    "dbo:alias",
    "dbp:name",
    "dbp:alternateName",
    "foaf:name",
    "dbp:otherNames",
    "dbp:otherName",
    "dbp:alternativeNames",
    "dbp:altname",
    "dbp:alias",
    "dbo:alternativeName",
    "foaf:nick",
    "dbp:commonName",
    "dbp:nativeName",
    "dbo:formerName",
    "dbp:synonyms",
    "dbp:synonym",
    "dbo:synonym",
]


def _response(names):
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "results": {"bindings": [{"name": {"type": "literal", "value": n}} for n in names]}
    }
    return resp


def _fetch(names, local_name="Turmeric"):
    client = SparqlClient("http://sparql.test", timeout=1, min_interval=0, max_retries=0)
    http = AsyncMock()
    http.post = AsyncMock(return_value=_response(names))
    result = asyncio.run(client.fetch_alt_names(http, local_name))
    query = http.post.await_args.kwargs["data"]["query"]
    return result, query


@pytest.mark.parametrize("prop", ALT_NAME_PROPERTIES)
def test_query_requests_every_alt_name_property(prop):
    _result, query = _fetch([])
    assert f" {prop} " in query


def test_query_targets_the_entity():
    _result, query = _fetch([], local_name="Mercury_(planet)")
    assert "<http://dbpedia.org/resource/Mercury_(planet)> ?p ?name" in query


def test_query_keeps_only_english_or_untagged_literals():
    _result, query = _fetch([])
    assert "isLiteral(?name)" in query
    assert "lang(?name) = ''" in query
    assert "langMatches(lang(?name), 'en')" in query


def test_bindings_become_name_strings():
    result, _query = _fetch(["Turmeric", "Curcuma domestica (Valeton)"])
    assert result == ["Turmeric", "Curcuma domestica (Valeton)"]


def test_no_bindings_gives_empty_list():
    result, _query = _fetch([])
    assert result == []
