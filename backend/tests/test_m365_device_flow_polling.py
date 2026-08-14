"""Tests for M365 device flow polling: single non-blocking check + correct
pending/failure error-code distinction.

Regression coverage:
  1. complete_device_flow() must check Microsoft's device flow endpoint
     exactly once per call (exit_condition=lambda f: True) instead of
     blocking the executor thread in MSAL's own internal retry loop for the
     flow's full lifetime (up to `expires_in`, ~15 min) — the frontend
     already polls this endpoint on an interval, so MSAL's own polling would
     otherwise pile up one blocked thread per poll, exhausting the
     executor's thread pool.
  2. The raised M365TokenError carries the OAuth error *code*
     (e.g. "authorization_pending") separately from the human-readable
     description, so /me/connect/status can reliably detect "still
     pending" instead of hoping the description happens to contain that
     substring.
"""

from __future__ import annotations

import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


def _install_fake_msal(acquire_result: dict, cache_serialized: str = "{}"):
    """Install a fake `msal` module and return the dict it records calls into."""
    fake_msal = types.ModuleType("msal")
    captured: dict = {}

    class _FakeCache:
        has_state_changed = False

        def serialize(self):
            return cache_serialized

        def deserialize(self, data):
            pass

    class _FakeApp:
        def __init__(self, client_id, authority=None, token_cache=None):
            captured["init"] = {"client_id": client_id, "authority": authority}

        def acquire_token_by_device_flow(self, flow, **kwargs):
            captured["flow"] = flow
            captured["kwargs"] = kwargs
            return acquire_result

    fake_msal.PublicClientApplication = _FakeApp
    fake_msal.SerializableTokenCache = _FakeCache
    sys.modules["msal"] = fake_msal
    return captured


def _make_db(scalar=None):
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = scalar
    db = AsyncMock()
    db.execute.return_value = result_mock
    db.add = MagicMock()  # AsyncSession.add() is synchronous, unlike execute/commit/refresh
    return db


class CompleteDeviceFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._orig_msal = sys.modules.get("msal")

    def tearDown(self):
        if self._orig_msal is not None:
            sys.modules["msal"] = self._orig_msal
        else:
            sys.modules.pop("msal", None)

    async def test_polls_microsoft_exactly_once_per_call(self):
        captured = _install_fake_msal(
            {"error": "authorization_pending", "error_description": "AADSTS70016: Pending end-user authorization."}
        )
        from hermeshq.services.m365_oauth import M365TokenError, complete_device_flow

        flow_state = {
            "_config": {"client_id": "cid", "authority": "https://login.microsoftonline.com/tenant"},
            "_flow": {"device_code": "dc", "user_code": "uc"},
        }
        with self.assertRaises(M365TokenError):
            await complete_device_flow(flow_state, MagicMock(), _make_db(), "user-1")

        exit_condition = captured["kwargs"]["exit_condition"]
        # A single call to exit_condition returning True means MSAL's internal
        # loop performs one check and returns instead of sleeping/retrying.
        self.assertTrue(exit_condition(flow_state["_flow"]))

    async def test_pending_error_carries_authorization_pending_code(self):
        _install_fake_msal(
            {"error": "authorization_pending", "error_description": "AADSTS70016: Pending end-user authorization."}
        )
        from hermeshq.services.m365_oauth import M365TokenError, complete_device_flow

        flow_state = {
            "_config": {"client_id": "cid", "authority": "https://login.microsoftonline.com/tenant"},
            "_flow": {"device_code": "dc", "user_code": "uc"},
        }
        with self.assertRaises(M365TokenError) as ctx:
            await complete_device_flow(flow_state, MagicMock(), _make_db(), "user-1")

        self.assertEqual(ctx.exception.error_code, "authorization_pending")
        # The human-readable description must still be preserved for logging.
        self.assertIn("Pending end-user authorization", str(ctx.exception))

    async def test_real_failure_carries_its_own_code_not_pending(self):
        _install_fake_msal({"error": "invalid_grant", "error_description": "The device code has expired."})
        from hermeshq.services.m365_oauth import M365TokenError, complete_device_flow

        flow_state = {
            "_config": {"client_id": "cid", "authority": "https://login.microsoftonline.com/tenant"},
            "_flow": {"device_code": "dc", "user_code": "uc"},
        }
        with self.assertRaises(M365TokenError) as ctx:
            await complete_device_flow(flow_state, MagicMock(), _make_db(), "user-1")

        self.assertEqual(ctx.exception.error_code, "invalid_grant")

    async def test_success_persists_token_record(self):
        _install_fake_msal(
            {
                "access_token": "tok-123",
                "id_token_claims": {"preferred_username": "user@contoso.com", "name": "User Name"},
                "scope": "openid profile email User.Read Mail.Read",
                "expires_in": 3600,
            }
        )
        from hermeshq.services.m365_oauth import complete_device_flow

        flow_state = {
            "_config": {"client_id": "cid", "authority": "https://login.microsoftonline.com/tenant"},
            "_flow": {"device_code": "dc", "user_code": "uc"},
        }
        vault = MagicMock()
        vault.encrypt.return_value = "encrypted-cache"
        db = _make_db(scalar=None)

        token_record = await complete_device_flow(flow_state, vault, db, "user-1")

        self.assertEqual(token_record.account_email, "user@contoso.com")
        self.assertEqual(token_record.user_id, "user-1")
        db.add.assert_called_once()
        db.commit.assert_awaited()


class PollConnectStatusTests(unittest.IsolatedAsyncioTestCase):
    """poll_connect_status must classify by error *code*, not description text."""

    async def _poll(self, side_effect):
        from hermeshq.routers import m365 as m365_router

        user = SimpleNamespace(id="user-1")
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(secret_vault=MagicMock())))
        db = AsyncMock()
        m365_router._pending_flows["user-1"] = {"_flow": {}, "_config": {}}
        try:
            with patch.object(m365_router, "complete_device_flow", AsyncMock(side_effect=side_effect)):
                return await m365_router.poll_connect_status(request, current_user=user, db=db)
        finally:
            m365_router._pending_flows.pop("user-1", None)

    async def test_pending_code_maps_to_pending_status_regardless_of_wording(self):
        from hermeshq.services.m365_oauth import M365TokenError

        # Description deliberately does NOT contain the literal string
        # "authorization_pending" — only the error code does. Before the
        # fix, complete_device_flow discarded the code in favour of the
        # description, so this case would incorrectly surface as a failure.
        error = M365TokenError("Autenticación fallida: Esperando que el usuario autorice.", error_code="authorization_pending")
        result = await self._poll(error)
        self.assertEqual(result.status, "pending")

    async def test_real_failure_raises_http_400(self):
        from fastapi import HTTPException

        from hermeshq.services.m365_oauth import M365TokenError

        error = M365TokenError("Autenticación fallida: El código expiró.", error_code="expired_token")
        with self.assertRaises(HTTPException) as ctx:
            await self._poll(error)
        self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
