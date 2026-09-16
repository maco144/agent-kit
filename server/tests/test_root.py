"""The root path points visitors at the docs and health check instead of a bare 404."""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from app.main import app


async def test_root_describes_the_service():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        resp = await anon.get("/")  # no API key: this route is public

    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "agent-kit Cloud"
    assert body["docs"] == "/docs"
    assert body["health"] == "/healthz"
    assert body["version"]
