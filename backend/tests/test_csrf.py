from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermeshq.core.csrf import CSRFMiddleware


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(CSRFMiddleware)

    @app.post("/api/change")
    async def change():
        return {"ok": True}

    @app.post("/api/auth/login")
    async def login():
        return {"ok": True}

    return app


def test_cookie_authenticated_mutation_requires_csrf_header() -> None:
    client = TestClient(_app())
    client.cookies.set("hermeshq_token", "token")
    client.cookies.set("hermeshq_csrf", "csrf")
    assert client.post("/api/change").status_code == 403
    assert client.post("/api/change", headers={"X-CSRF-Token": "csrf"}).status_code == 200


def test_bearer_and_public_auth_requests_are_not_subject_to_cookie_csrf() -> None:
    client = TestClient(_app())
    client.cookies.set("hermeshq_token", "token")
    assert client.post("/api/change", headers={"Authorization": "Bearer token"}).status_code == 200
    assert client.post("/api/auth/login").status_code == 403
    client.cookies.clear()
    assert client.post("/api/auth/login").status_code == 200
