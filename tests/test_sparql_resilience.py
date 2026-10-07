import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from app.sparql_client import SparqlClient, SparqlTransientError


def _ok_response():
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"results": {"bindings": []}}
    return resp


def test_read_timeout_is_retried_then_succeeds():
    client = SparqlClient("http://sparql.test", timeout=1, min_interval=0, max_retries=3)
    http = AsyncMock()
    http.post = AsyncMock(side_effect=[httpx.ReadTimeout("slow"), _ok_response()])

    async def run():
        with patch("app.sparql_client.asyncio.sleep", new=AsyncMock()):
            return await client._run(http, "SELECT * WHERE { ?s ?p ?o }")

    assert asyncio.run(run()) == []
    assert http.post.await_count == 2


def test_repeated_read_timeout_becomes_transient_error():
    client = SparqlClient("http://sparql.test", timeout=1, min_interval=0, max_retries=3)
    http = AsyncMock()
    http.post = AsyncMock(side_effect=httpx.ReadTimeout("slow"))

    async def run():
        with patch("app.sparql_client.asyncio.sleep", new=AsyncMock()):
            await client._run(http, "SELECT * WHERE { ?s ?p ?o }")

    try:
        asyncio.run(run())
    except SparqlTransientError as exc:
        assert "failed after" in str(exc)
    else:
        raise AssertionError("expected SparqlTransientError")
    assert http.post.await_count == 2
