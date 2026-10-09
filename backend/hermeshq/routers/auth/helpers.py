"""Shared state, constants, and cross-cutting helpers for the auth router package.

No endpoints are defined here — see ``local.py``, ``mfa.py`` and ``oidc.py``.
"""

import logging
import re
import secrets
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.config import get_settings
from hermeshq.models.user import User
from hermeshq.schemas.auth import UserRead
from hermeshq.services.avatar import (
    build_avatar_path as _build_avatar_path_shared,
)

# Shared httpx client for connection pooling (reused across OIDC calls)
_http_client: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    """Return the shared httpx client, creating it if needed."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=15.0)
    return _http_client


# ---------------------------------------------------------------------------
# Rate limiter for login endpoint
# ---------------------------------------------------------------------------
_LOGIN_MAX_ATTEMPTS = 10
_LOGIN_WINDOW_SECONDS = 300  # 5 minutes
_login_attempts: dict[str, list[float]] = defaultdict(list)


def _check_login_rate(ip: str) -> None:
    """Raise 429 if IP has exceeded login rate limit."""
    now = time.time()
    attempts = _login_attempts.get(ip, [])
    # Prune expired attempts
    recent = [t for t in attempts if now - t < _LOGIN_WINDOW_SECONDS]
    if recent:
        _login_attempts[ip] = recent
    elif ip in _login_attempts:
        # Clean up empty entries to prevent unbounded memory growth
        del _login_attempts[ip]
    if len(_login_attempts.get(ip, [])) >= _LOGIN_MAX_ATTEMPTS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts. Please try again later.",
        )


def _record_login_attempt(ip: str) -> None:
    """Record a failed login attempt for rate limiting."""
    _login_attempts.setdefault(ip, []).append(time.time())


logger = logging.getLogger(__name__)

_JWKS_CACHE: dict = {"keys": None, "fetched_at": 0.0}
_JWKS_CACHE_TTL = 3600  # 1 hour in seconds

AUTH_MODE_LOCAL = "local"
AUTH_MODE_HYBRID = "hybrid"
AUTH_MODE_OIDC = "oidc"
OIDC_STATE_EXPIRY_MINUTES = 10
USERNAME_SANITIZER = re.compile(r"[^a-z0-9._-]+")
DEFAULT_OIDC_PROVIDER_LABELS = {
    "google": "Google",
    "microsoft": "Microsoft",
}
PUBLIC_ENTERPRISE_PROVIDERS = ("google", "microsoft")

# ---------------------------------------------------------------------------
# MFA configuration helpers
# ---------------------------------------------------------------------------
MFA_CODE_EXPIRY_MINUTES = 5
MFA_CODE_MAX_ATTEMPTS = 5  # max verification attempts per code
MFA_RESEND_COOLDOWN_SECONDS = 30

COOKIE_NAME = "hermeshq_token"
CSRF_COOKIE_NAME = "hermeshq_csrf"


def _set_auth_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    max_age = settings.access_token_minutes * 60
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=max_age,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=secrets.token_urlsafe(32),
        max_age=max_age,
        httponly=False,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def _clear_auth_cookie(response: Response) -> None:
    settings = get_settings()
    for name, httponly in ((COOKIE_NAME, True), (CSRF_COOKIE_NAME, False)):
        response.set_cookie(
            key=name,
            value="",
            max_age=0,
            httponly=httponly,
            secure=settings.cookie_secure,
            samesite="lax",
            path="/",
        )


def _serialize_user(request: Request, user: User) -> UserRead:
    payload = UserRead.model_validate(user)
    avatar_url = None
    if user.avatar_filename:
        version = int(user.updated_at.timestamp()) if user.updated_at else 0
        avatar_url = f"/api/users/{user.id}/avatar?v={version}"
    return payload.model_copy(update={"avatar_url": avatar_url, "has_avatar": bool(user.avatar_filename)})


def _get_auth_mode() -> str:
    mode = (get_settings().auth_mode or AUTH_MODE_LOCAL).strip().lower()
    if mode not in {AUTH_MODE_LOCAL, AUTH_MODE_HYBRID, AUTH_MODE_OIDC}:
        return AUTH_MODE_LOCAL
    return mode


def _validate_redirect_host(forwarded_host: str) -> bool:
    """Validate that an X-Forwarded-Host header matches an allowed origin.

    Uses the configured cors_origins list as the trusted-host whitelist.
    Only the hostname (without port) is checked so that different ports on
    the same domain are still accepted.
    """
    from urllib.parse import urlparse as _urlparse

    allowed_hosts: set[str] = set()
    for origin in get_settings().cors_origins:
        try:
            parsed = _urlparse(origin)
            if parsed.hostname:
                allowed_hosts.add(parsed.hostname)
        except (ValueError, TypeError):
            continue
    # Extract hostname from the forwarded value (may include port)
    candidate = forwarded_host.split(":")[0]
    return candidate in allowed_hosts


def _build_frontend_redirect(request: Request, *, oidc_complete: bool = False, auth_error: str | None = None) -> str:
    forwarded_host = request.headers.get("x-forwarded-host")
    if forwarded_host and not _validate_redirect_host(forwarded_host):
        logger.warning("Rejected X-Forwarded-Host %r — not in allowed origins", forwarded_host)
        forwarded_host = None
    host = forwarded_host or request.headers.get("host") or request.url.netloc
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    base_url = f"{scheme}://{host}/"
    if oidc_complete:
        # The JWT travels only in the httpOnly cookie — never in the URL
        # (browser history, proxy logs, Referer). The SPA exchanges the
        # cookie for a token via POST /auth/refresh.
        return f"{base_url}login?oidc=complete"
    if auth_error:
        # Failed auth: redirect to /login so LoginPage.tsx can display the error
        query = urlencode({"auth_error": auth_error})
        return f"{base_url}login?{query}"
    return base_url


def _normalize_email(email: str | None) -> str | None:
    if not email:
        return None
    normalized = email.strip().lower()
    return normalized or None


async def _get_local_user_by_email(db: AsyncSession, email: str | None) -> User | None:
    if not email:
        return None
    result = await db.execute(select(User).where(func.lower(User.email) == email.lower()))
    return result.scalars().first()


def _user_avatar_base() -> Path:
    return Path(get_settings().user_assets_root)


def _build_avatar_path(user: User) -> Path | None:
    return _build_avatar_path_shared(_user_avatar_base(), user.id, user.avatar_filename)
