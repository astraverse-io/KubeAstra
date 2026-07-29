"""Desktop-mode security boundary.

These cover the localhost threat model: a web page the user visits trying to
reach the loopback API, and DNS-rebinding. The boundary is exercised against a
minimal app rather than the full backend so the tests stay fast and do not
need MCP/Qdrant/kubeconfig.
"""

import importlib
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

TOKEN = "test-token-abc123"
PORT = "51999"
ORIGIN = f"http://127.0.0.1:{PORT}"


@pytest.fixture
def desktop(monkeypatch, tmp_path):
    """Fresh desktop_security module bound to a known token/port."""
    monkeypatch.setenv("KUBEASTRA_DESKTOP_TOKEN", TOKEN)
    monkeypatch.setenv("KUBEASTRA_DESKTOP_PORT", PORT)

    import desktop_security

    module = importlib.reload(desktop_security)

    static_root = tmp_path / "out"
    (static_root / "chat").mkdir(parents=True)
    (static_root / "chat" / "index.html").write_text("<html>chat</html>")
    (static_root / "_next").mkdir()
    (static_root / "index.html").write_text("<html>home</html>")

    app = FastAPI()

    @app.get("/api/sessions")
    async def sessions():
        return {"sessions": []}

    @app.post("/api/v1/alerts/webhook")
    async def webhook():
        return {"ok": True}

    @app.get("/metrics")
    async def metrics():
        return "metrics"

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    module.install(app, static_root=static_root)
    return module, TestClient(app, base_url=ORIGIN)


# ── public vs protected ────────────────────────────────────────────────────


def test_health_is_public(desktop):
    _, client = desktop
    assert client.get("/health").status_code == 200


def test_api_requires_token(desktop):
    _, client = desktop
    assert client.get("/api/sessions").status_code == 401


def test_metrics_requires_token(desktop):
    """Server mode exempts /metrics for Prometheus; desktop must not."""
    _, client = desktop
    assert client.get("/metrics").status_code == 401


def test_alerts_webhook_requires_token(desktop):
    """auth.is_public_path exempts this route in server mode. On a laptop it
    would let any page trigger an investigation."""
    _, client = desktop
    assert client.post("/api/v1/alerts/webhook", json={}).status_code == 401


def test_static_pages_are_public(desktop):
    """The export is inert open-source HTML and must load before auth."""
    module, _ = desktop
    assert module.is_public_path("/chat/")
    assert module.is_public_path("/_next/static/chunk.js")
    assert module.is_public_path("/")


def test_api_never_treated_as_static(desktop):
    module, _ = desktop
    assert not module.is_public_path("/api/sessions")
    assert not module.is_public_path("/metrics")


# ── token acceptance ───────────────────────────────────────────────────────


def test_bearer_token_grants_access(desktop):
    _, client = desktop
    response = client.get("/api/sessions", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200


def test_wrong_token_rejected(desktop):
    _, client = desktop
    response = client.get("/api/sessions", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401


def test_auth_exchange_sets_cookie_and_redirects(desktop):
    module, client = desktop
    response = client.get(f"/auth?token={TOKEN}", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/"
    cookie = response.cookies.get(module.cookie_name())
    assert cookie == TOKEN


def test_auth_exchange_rejects_bad_token(desktop):
    _, client = desktop
    assert client.get("/auth?token=wrong", follow_redirects=False).status_code == 401


def test_cookie_grants_access_after_exchange(desktop):
    _, client = desktop
    client.get(f"/auth?token={TOKEN}", follow_redirects=False)
    assert client.get("/api/sessions").status_code == 200


def test_cookie_name_is_port_scoped(desktop):
    module, _ = desktop
    assert module.cookie_name() == f"kubeastra_desktop_{PORT}"


# ── browser threat model ───────────────────────────────────────────────────


def test_foreign_host_rejected(desktop):
    """DNS rebinding: attacker.com resolving to 127.0.0.1."""
    _, client = desktop
    response = client.get("/health", headers={"Host": "attacker.com"})
    assert response.status_code == 403


def test_foreign_origin_rejected(desktop):
    _, client = desktop
    response = client.get(
        "/api/sessions",
        headers={"Authorization": f"Bearer {TOKEN}", "Origin": "https://evil.example"},
    )
    assert response.status_code == 403


def test_other_localhost_port_rejected(desktop):
    """Cookies are not port-scoped, so a page on another local port could ride
    our cookie. Origin carries the port and is the real defense."""
    _, client = desktop
    client.get(f"/auth?token={TOKEN}", follow_redirects=False)
    response = client.get("/api/sessions", headers={"Origin": "http://127.0.0.1:3000"})
    assert response.status_code == 403


def test_own_origin_allowed(desktop):
    _, client = desktop
    response = client.get(
        "/api/sessions",
        headers={"Authorization": f"Bearer {TOKEN}", "Origin": ORIGIN},
    )
    assert response.status_code == 200


def test_missing_origin_allowed_with_token(desktop):
    """curl and local scripts send no Origin; they still need the token."""
    _, client = desktop
    response = client.get("/api/sessions", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200


def test_html_navigation_redirects_instead_of_json_401(desktop):
    """A stale bookmark should land on the app, not a JSON error page."""
    _, client = desktop
    response = client.get(
        "/api/sessions", headers={"Accept": "text/html"}, follow_redirects=False
    )
    assert response.status_code == 302
    assert response.headers["location"] == "/"
