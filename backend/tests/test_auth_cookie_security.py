from types import SimpleNamespace
from unittest.mock import patch

from fastapi import Response
from fastapi.responses import RedirectResponse

from hermeshq.routers.auth.helpers import _clear_auth_cookie, _set_auth_cookie
from hermeshq.routers.auth.oidc import _clear_oidc_state_cookie, _set_oidc_state_cookie


def _cookie_headers(response: Response) -> list[str]:
    return [value.decode("latin-1") for name, value in response.raw_headers if name.lower() == b"set-cookie"]


def test_auth_cookies_use_secure_production_attributes() -> None:
    settings = SimpleNamespace(access_token_minutes=60, cookie_secure=True)
    response = Response()

    with patch("hermeshq.routers.auth.helpers.get_settings", return_value=settings):
        _set_auth_cookie(response, "token")

    auth_cookie, csrf_cookie = _cookie_headers(response)
    assert auth_cookie.startswith("hermeshq_token=token;")
    assert "HttpOnly" in auth_cookie
    assert "Path=/" in auth_cookie
    assert "SameSite=lax" in auth_cookie
    assert "Secure" in auth_cookie
    assert csrf_cookie.startswith("hermeshq_csrf=")
    assert "HttpOnly" not in csrf_cookie
    assert "Path=/" in csrf_cookie
    assert "SameSite=lax" in csrf_cookie
    assert "Secure" in csrf_cookie


def test_cookie_deletions_preserve_secure_attributes() -> None:
    settings = SimpleNamespace(cookie_secure=True)
    auth_response = Response()
    oidc_response = RedirectResponse("https://example.test")

    with (
        patch("hermeshq.routers.auth.helpers.get_settings", return_value=settings),
        patch("hermeshq.routers.auth.oidc.get_settings", return_value=settings),
    ):
        _clear_auth_cookie(auth_response)
        _set_oidc_state_cookie(oidc_response, "state")
        _clear_oidc_state_cookie(oidc_response)

    for header in _cookie_headers(auth_response) + _cookie_headers(oidc_response):
        assert "SameSite=lax" in header
        assert "Secure" in header
