"""A sentence is matched against indexed synonym phrases, not loose tokens."""
import copy
import json

import httpx
import pytest_asyncio
from httpx import ASGITransport
from starlette.datastructures import State

from app.main import app
from app.synonym_index import surface_hits

BEN = {
    "entity_url": "DBPedia>Ben_oil",
    "entity": "Ben_oil",
    "seed_category": "Category:Food_ingredients",
    "surface_text": [
        {"text": "ben oil", "how_this_record": "rdfs:label"},
        {"text": "behen oil", "how_this_record": "redirect"},
    ],
}
OIL = {
    "entity_url": "DBPedia>Oil",
    "entity": "Oil",
    "seed_category": "Category:Food_ingredients",
    "surface_text": [{"text": "oil", "how_this_record": "rdfs:label"}],
}


def test_whole_phrase_matches_and_shorter_word_inside_a_word_does_not():
    matches = surface_hits("Heat the behen oil before boiling.", [BEN, OIL])
    by_entity = {match["entity"]: match for match in matches}

    assert set(by_entity) == {"Ben_oil", "Oil"}
    assert by_entity["Ben_oil"]["surface_text"] == [
        {"text": "behen oil", "how_this_record": "redirect", "start": 9, "end": 18}
    ]
    assert by_entity["Oil"]["surface_text"] == [
        {"text": "oil", "how_this_record": "rdfs:label", "start": 15, "end": 18}
    ]


def test_no_entity_when_nothing_overlaps():
    assert surface_hits("just water", [BEN, OIL]) == []


def test_typo_is_ignored_unless_fuzzy_is_requested():
    assert surface_hits("use ben oill today", [BEN]) == []

    matches = surface_hits("use ben oill today", [BEN, OIL], fuzzy=True)
    by_entity = {match["entity"]: match for match in matches}

    assert by_entity["Ben_oil"]["surface_text"][0]["text"] == "ben oil"
    assert by_entity["Ben_oil"]["surface_text"][0]["matched_text"] == "ben oill"
    assert by_entity["Oil"]["surface_text"] == [
        {"text": "oil", "how_this_record": "rdfs:label", "start": 8, "end": 12, "matched_text": "oill"}
    ]
    assert surface_hits("boiling", [OIL], fuzzy=True) == []


class _SearchMock:
    def __init__(self, *, fail=None):
        self.fail = fail
        self.docs = [BEN, OIL]
        self.queries = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.fail == "connect":
            raise httpx.ConnectError("connection refused")
        if request.method == "POST" and request.url.path.endswith("/_search"):
            self.queries.append(json.loads(request.content))
            hits = [
                {"_id": str(index), "_source": doc, "sort": [str(index)]}
                for index, doc in enumerate(self.docs)
            ]
            return httpx.Response(200, json={"hits": {"total": {"value": len(hits)}, "hits": hits}})
        return httpx.Response(500, text=f"unexpected {request.method} {request.url.path}")


@pytest_asyncio.fixture
async def match_api():
    mock = _SearchMock()
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(mock.handler))
    local_app = copy.copy(app)
    local_app.state = State()
    local_app.state.http_client = http_client
    async with httpx.AsyncClient(transport=ASGITransport(app=local_app), base_url="http://test") as client:
        yield client, mock
    await http_client.aclose()


async def test_match_endpoint_returns_entities(match_api):
    client, _ = match_api
    response = await client.post(
        "/api/v1/synonyms/match",
        json={"sentence": "Heat the behen oil before boiling."},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["elasticsearch_available"] is True
    assert [match["entity"] for match in body["matches"]] == ["Ben_oil", "Oil"]
    assert body["fuzzy"] is False
    assert "fuzziness" not in match_api[1].queries[0]["query"]["match"]["surface_text.text"]


async def test_fuzzy_flag_is_sent_to_elasticsearch_and_matches_a_typo(match_api):
    client, mock = match_api
    response = await client.post(
        "/api/v1/synonyms/match",
        json={"sentence": "use ben oill today", "fuzzy": True},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["fuzzy"] is True
    assert mock.queries[0]["query"]["match"]["surface_text.text"]["fuzziness"] == "AUTO"
    assert [match["entity"] for match in body["matches"]] == ["Ben_oil", "Oil"]


async def test_match_endpoint_reports_elasticsearch_down(match_api):
    client, mock = match_api
    mock.fail = "connect"
    response = await client.post(
        "/api/v1/synonyms/match",
        json={"sentence": "behen oil"},
    )
    assert response.status_code == 503
    body = response.json()
    assert body["elasticsearch_available"] is False
    assert body["matches"] == []
    assert "not available" in body["detail"]


async def test_match_endpoint_rejects_a_blank_sentence(match_api):
    client, _ = match_api
    response = await client.post("/api/v1/synonyms/match", json={"sentence": "   "})
    assert response.status_code == 400
