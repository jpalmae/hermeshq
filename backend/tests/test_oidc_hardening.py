from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from hermeshq.services.oidc_provider import (
    _validate_id_token,
    build_authorization_url,
    create_oidc_state,
    resolve_or_create_user,
    verify_oidc_state,
)


def test_oidc_state_contains_signed_nonce() -> None:
    secret = "a-long-test-secret-that-is-at-least-32-bytes"
    state = create_oidc_state("corp", secret)
    payload = verify_oidc_state(state, secret)
    assert payload["provider"] == "corp"
    assert payload["nonce"]
    assert verify_oidc_state(state, "wrong-secret-that-is-also-at-least-32-bytes") is None
    assert verify_oidc_state(None, secret) is None


@pytest.mark.asyncio
async def test_authorization_url_binds_nonce(monkeypatch) -> None:
    provider = MagicMock(
        discovery_url="https://id.example.com",
        client_id="client-id",
        scopes="openid email",
    )
    monkeypatch.setattr(
        "hermeshq.services.oidc_provider._fetch_discovery",
        AsyncMock(return_value={"authorization_endpoint": "https://id.example.com/authorize"}),
    )
    url = await build_authorization_url(provider, "https://app.example.com/callback", "signed-state", "bound-nonce")
    assert "state=signed-state" in url
    assert "nonce=bound-nonce" in url


@pytest.mark.asyncio
async def test_id_token_validation_fails_closed_without_jwks() -> None:
    provider = MagicMock(slug="corp")
    with pytest.raises(ValueError, match="jwks_uri"):
        await _validate_id_token("token", provider, {}, "nonce")


@pytest.mark.asyncio
async def test_id_token_validation_fails_closed_with_empty_jwks(monkeypatch) -> None:
    provider = MagicMock(slug="corp")
    monkeypatch.setattr("hermeshq.services.oidc_provider._fetch_jwks", AsyncMock(return_value=[]))
    with pytest.raises(ValueError, match="no JWKS keys"):
        await _validate_id_token("token", provider, {"jwks_uri": "https://id.example.com/keys"}, "nonce")


@pytest.mark.asyncio
async def test_unverified_email_is_rejected_before_account_lookup() -> None:
    provider = MagicMock(allowed_domains=None)
    db = AsyncMock()
    with pytest.raises(HTTPException, match="not verified"):
        await resolve_or_create_user(
            db,
            {"sub": "subject", "email": "admin@example.com", "email_verified": False},
            provider,
        )
    db.execute.assert_not_awaited()
