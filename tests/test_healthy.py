from tests.test_helpers import Stub, dig

async def test_healthy(tmp_path):
    r = await dig(tmp_path, Stub())
    body = r.json()
    assert r.status_code == 200
    assert body["status"] == "complete"
    assert body["failed_requests"] == 0
    assert body["error"] is None
    assert {form["text"] for doc in body["data"] for form in doc["surface_text"]} == {
        "sodium chloride",
        "nacl",
    }