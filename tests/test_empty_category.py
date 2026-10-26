from tests.test_helpers import Stub, dig

async def test_really_empty_returns_complete_status(tmp_path):
    stub = Stub(articles=(), labels=(), redirects=())
    response = await dig(tmp_path, stub)
    body = response.json()

    assert response.status_code == 200
    assert body["data"] == []
    assert body["status"] == "complete"
    assert body["failed_requests"] == 0
    assert body["error"] is None