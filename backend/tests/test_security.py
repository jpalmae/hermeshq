"""Comprehensive unit tests for hermeshq.core.security."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hermeshq.core.security import (
    _resolve_token_from_request,
    create_access_token,
    create_agent_service_token,
    decode_access_token,
    decode_access_token_subject,
    hash_password,
    is_admin,
    verify_agent_service_token,
    verify_password,
)

# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------


class TestHashPassword(unittest.TestCase):
    """Tests for hash_password / verify_password."""

    def test_valid_password_verifies(self) -> None:
        hashed = hash_password("s3cret!")
        self.assertTrue(verify_password("s3cret!", hashed))

    def test_wrong_password_fails(self) -> None:
        hashed = hash_password("s3cret!")
        self.assertFalse(verify_password("wrong-password", hashed))

    def test_hash_is_not_plaintext(self) -> None:
        password = "my-plain-password"
        hashed = hash_password(password)
        self.assertNotEqual(hashed, password)
        # Argon2 hashes start with $argon2
        self.assertTrue(hashed.startswith("$argon2"))

    def test_different_passwords_produce_different_hashes(self) -> None:
        h1 = hash_password("alpha")
        h2 = hash_password("bravo")
        self.assertNotEqual(h1, h2)

    def test_same_password_produces_different_hashes(self) -> None:
        """Salting should make each hash unique."""
        h1 = hash_password("same")
        h2 = hash_password("same")
        self.assertNotEqual(h1, h2)


# ---------------------------------------------------------------------------
# JWT access tokens
# ---------------------------------------------------------------------------


class TestAccessToken(unittest.TestCase):
    """Tests for create_access_token / decode_access_token / decode_access_token_subject."""

    def test_round_trip_returns_same_subject(self) -> None:
        token, expires_at = create_access_token("user-42")
        subject = decode_access_token(token)
        self.assertEqual(subject, "user-42")

    def test_round_trip_preserves_subject_kind(self) -> None:
        token, _ = create_access_token("jdoe", subject_kind="username")
        sub, sub_kind = decode_access_token_subject(token)
        self.assertEqual(sub, "jdoe")
        self.assertEqual(sub_kind, "username")

    def test_default_subject_kind_is_id(self) -> None:
        token, _ = create_access_token("abc")
        _, sub_kind = decode_access_token_subject(token)
        self.assertEqual(sub_kind, "id")

    @patch("hermeshq.core.security.settings")
    def test_expired_token_returns_none(self, mock_settings) -> None:
        """Token created with negative TTL is already expired."""
        mock_settings.jwt_secret = "test-secret-for-expiry"
        mock_settings.jwt_algorithm = "HS256"
        mock_settings.access_token_minutes = -1
        import datetime as _dt

        from jose import jwt as jose_jwt

        expires_at = _dt.datetime.now(_dt.UTC) + _dt.timedelta(minutes=-1)
        token = jose_jwt.encode(
            {"sub": "expired-user", "sub_kind": "id", "exp": expires_at},
            mock_settings.jwt_secret,
            algorithm=mock_settings.jwt_algorithm,
        )
        # Use the real settings for decode (jwt_secret must match)
        with (
            patch.object(
                __import__("hermeshq.core.security", fromlist=["settings"]).settings,
                "jwt_secret",
                mock_settings.jwt_secret,
            ),
            patch.object(
                __import__("hermeshq.core.security", fromlist=["settings"]).settings,
                "jwt_algorithm",
                mock_settings.jwt_algorithm,
            ),
        ):
            result = decode_access_token(token)
        self.assertIsNone(result)

    def test_invalid_garbage_token_returns_none(self) -> None:
        self.assertIsNone(decode_access_token("not.a.real.token"))

    def test_completely_random_string_returns_none(self) -> None:
        self.assertIsNone(decode_access_token("garbage"))

    def test_empty_string_returns_none(self) -> None:
        self.assertIsNone(decode_access_token(""))

    def test_decode_subject_returns_none_none_for_garbage(self) -> None:
        sub, sub_kind = decode_access_token_subject("$$$invalid$$$")
        self.assertIsNone(sub)
        self.assertIsNone(sub_kind)

    def test_create_returns_expires_at_datetime(self) -> None:
        import datetime as _dt

        _, expires_at = create_access_token("x")
        self.assertIsInstance(expires_at, _dt.datetime)


# ---------------------------------------------------------------------------
# Agent service token (HMAC-SHA256)
# ---------------------------------------------------------------------------


class TestAgentServiceToken(unittest.TestCase):
    """Tests for create_agent_service_token."""

    def test_token_validates_for_agent_and_version(self) -> None:
        token = create_agent_service_token("agent-007", 3)
        self.assertTrue(verify_agent_service_token(token, "agent-007", 3))

    def test_different_agent_id_different_token(self) -> None:
        tok1 = create_agent_service_token("agent-001")
        tok2 = create_agent_service_token("agent-002")
        self.assertNotEqual(tok1, tok2)

    def test_version_rotation_revokes_token(self) -> None:
        tok = create_agent_service_token("agent-x")
        self.assertFalse(verify_agent_service_token(tok, "agent-x", 2))


# ---------------------------------------------------------------------------
# is_admin
# ---------------------------------------------------------------------------


class TestIsAdmin(unittest.TestCase):
    """Tests for is_admin — uses SimpleNamespace to avoid SQLAlchemy mapper resolution."""

    def test_admin_user_returns_true(self) -> None:
        user = SimpleNamespace(role="admin", is_active=True)
        self.assertTrue(is_admin(user))

    def test_regular_user_returns_false(self) -> None:
        user = SimpleNamespace(role="user", is_active=True)
        self.assertFalse(is_admin(user))

    def test_user_with_no_role_defaults_to_user(self) -> None:
        """When user.role is None the function should treat it as 'user' and return False."""
        user = SimpleNamespace(role=None, is_active=True)
        self.assertFalse(is_admin(user))

    def test_custom_role_returns_false(self) -> None:
        user = SimpleNamespace(role="moderator", is_active=True)
        self.assertFalse(is_admin(user))


# ---------------------------------------------------------------------------
# _resolve_token_from_request
# ---------------------------------------------------------------------------


class TestResolveTokenFromRequest(unittest.IsolatedAsyncioTestCase):
    """Tests for _resolve_token_from_request."""

    async def test_bearer_token_returned(self) -> None:
        result = await _resolve_token_from_request(bearer_token="bearer-val", cookie_token=None)
        self.assertEqual(result, "bearer-val")

    async def test_cookie_token_when_no_bearer(self) -> None:
        result = await _resolve_token_from_request(bearer_token=None, cookie_token="cookie-val")
        self.assertEqual(result, "cookie-val")

    async def test_empty_string_when_neither(self) -> None:
        result = await _resolve_token_from_request(bearer_token=None, cookie_token=None)
        self.assertEqual(result, "")

    async def test_bearer_takes_precedence_over_cookie(self) -> None:
        result = await _resolve_token_from_request(bearer_token="from-header", cookie_token="from-cookie")
        self.assertEqual(result, "from-header")

    async def test_empty_strings_yield_empty(self) -> None:
        """Empty strings are falsy, so the function should return ''."""
        result = await _resolve_token_from_request(bearer_token="", cookie_token="")
        self.assertEqual(result, "")


if __name__ == "__main__":
    unittest.main()
