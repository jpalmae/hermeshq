"""Tests for the enrollment service and router (C1)."""

from __future__ import annotations

import pytest

from hermeshq.core.security import create_device_token, decode_device_token_claims
from hermeshq.services.enrollment import (
    EnrollmentError,
    EnrollmentService,
    hash_enroll_token,
    parse_fail_mode,
)


def test_parse_fail_mode_valid_modes():
    assert parse_fail_mode("fail-open") == ("fail-open", 0)
    assert parse_fail_mode("fail-closed") == ("fail-closed", 0)
    assert parse_fail_mode("fail-grace:60") == ("fail-grace", 60)


def test_parse_fail_mode_invalid():
    assert parse_fail_mode("nonsense") == ("fail-open", 0)
    assert parse_fail_mode("fail-grace:notanumber") == ("fail-open", 0)
    assert parse_fail_mode(None) == ("fail-open", 0)


def test_hash_enroll_token_stable():
    assert hash_enroll_token("abc") == hash_enroll_token("abc")
    assert hash_enroll_token("abc") != hash_enroll_token("abd")


def test_device_token_claims_roundtrip():
    token = create_device_token("agent-1", "device-1", 3)
    claims = decode_device_token_claims(token)
    assert claims is not None
    assert claims["sub"] == "agent-1"
    assert claims["did"] == "device-1"
    assert claims["token_version"] == 3


def test_device_token_claims_rejects_agent_token():
    from hermeshq.core.security import create_agent_service_token

    agent_token = create_agent_service_token("agent-1", 1)
    assert decode_device_token_claims(agent_token) is None


class TestEnrollmentService:
    @pytest.fixture
    def service(self):
        return EnrollmentService(session_factory=None, enforcer=None)

    def test_revoke_with_no_backend_raises(self, service):
        import asyncio

        with pytest.raises(Exception):
            asyncio.run(service.revoke_device("missing"))


def test_telemetry_task_id_is_deterministic():
    from hermeshq.routers.internal_control import _telemetry_task_id

    a = _telemetry_task_id("agent-1", "turn-9")
    b = _telemetry_task_id("agent-1", "turn-9")
    c = _telemetry_task_id("agent-1", "turn-10")
    d = _telemetry_task_id("agent-2", "turn-9")
    assert a == b
    assert a != c
    assert a != d


def test_telemetry_task_id_without_turn_is_unique():
    from hermeshq.routers.internal_control import _telemetry_task_id

    assert _telemetry_task_id("agent-1", None) != _telemetry_task_id("agent-1", None)
