import pytest

from hermeshq.config import Settings


def test_production_rejects_missing_jwt_secret() -> None:
    with pytest.raises(RuntimeError, match="JWT_SECRET is not set"):
        Settings(
            _env_file=None,
            debug=False,
            jwt_secret="",
            fernet_key="independent-fernet-seed",
            admin_password="strong-admin-password",
        )


def test_production_rejects_missing_fernet_key() -> None:
    with pytest.raises(RuntimeError, match="FERNET_KEY is not set"):
        Settings(
            _env_file=None,
            debug=False,
            jwt_secret="strong-jwt-secret-that-is-long-enough",
            fernet_key=None,
            admin_password="strong-admin-password",
        )


def test_production_rejects_weak_admin_password() -> None:
    with pytest.raises(RuntimeError, match="ADMIN_PASSWORD is empty, too short"):
        Settings(
            _env_file=None,
            debug=False,
            jwt_secret="strong-jwt-secret-that-is-long-enough",
            fernet_key="independent-fernet-seed",
            admin_password="short",
        )
