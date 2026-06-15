import hashlib
import logging
import re
import secrets
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse, urlunparse

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from jose import JWTError, jwt
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
import httpx

# Shared httpx client for connection pooling (reused across OIDC calls)
_http_client: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    """Return the shared httpx client, creating it if needed."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=15.0)
    return _http_client


from hermeshq.core.security import (
    create_access_token,
    get_current_user,
    hash_password,
    verify_password,
)
from hermeshq.config import get_settings
from hermeshq.database import get_db_session
from hermeshq.models.user import User
from hermeshq.models.password_reset import PasswordResetToken
from hermeshq.models.mfa_code import MfaCode
from hermeshq.models.app_settings import AppSettings
from hermeshq.schemas.auth import (
    AuthProviderRead,
    AuthProvidersResponse,
    ChangePasswordRequest,
    ForgotPasswordRequest,
    ResetPasswordRequest,
    PasswordResetResponse,
    EmailConfigStatus,
    LoginRequest,
    TokenResponse,
    UserPreferencesUpdate,
    UserProfileUpdate,
    UserRead,
    MfaRequiredResponse,
    MfaVerifyRequest,
    MfaResendRequest,
    MfaStatusResponse,
)
from hermeshq.services.email_service import get_email_service, EmailServiceError
from hermeshq.services.avatar import (
    build_avatar_path as _build_avatar_path_shared,
    delete_avatar_files as _delete_avatar_files_shared,
    validate_and_save_avatar,
    resolve_media_type,
)

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

router = APIRouter(prefix="/auth", tags=["auth"])
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


async def _is_mfa_globally_enabled(db: AsyncSession) -> bool:
    """Check if MFA via email is enabled in global app settings."""
    result = await db.execute(select(AppSettings).where(AppSettings.id == "default"))
    app_settings = result.scalar_one_or_none()
    if not app_settings:
        return False
    return bool(app_settings.mfa_email_enabled)


def _create_mfa_token(user_id: str) -> tuple[str, datetime]:
    """Create a short-lived JWT token for MFA verification step."""
    settings = get_settings()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=MFA_CODE_EXPIRY_MINUTES)
    payload = {
        "sub": user_id,
        "sub_kind": "id",
        "mfa_pending": True,
        "exp": expires_at,
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, expires_at


def _verify_mfa_token(mfa_token: str) -> str | None:
    """Verify a MFA token and return the user_id, or None if invalid."""
    try:
        settings = get_settings()
        payload = jwt.decode(mfa_token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        if not payload.get("mfa_pending"):
            return None
        return payload.get("sub")
    except JWTError:
        return None


def _generate_mfa_code() -> str:
    """Generate a random 6-digit code."""
    return f"{secrets.randbelow(1_000_000):06d}"


def _mask_email(email: str | None) -> str | None:
    """Mask email for display: a***@domain.com"""
    if not email or "@" not in email:
        return email
    local, domain = email.rsplit("@", 1)
    if len(local) <= 2:
        masked_local = local[0] + "***"
    else:
        masked_local = local[0] + "***" + local[-1]
    return f"{masked_local}@{domain}"


async def _send_mfa_code(
    db: AsyncSession,
    user: User,
    client_ip: str | None,
) -> str:
    """Generate and send an MFA code to the user's email. Returns the raw code for verification."""
    raw_code = _generate_mfa_code()
    code_hash = hashlib.sha256(raw_code.encode()).hexdigest()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=MFA_CODE_EXPIRY_MINUTES)

    mfa_code = MfaCode(
        user_id=user.id,
        code_hash=code_hash,
        expires_at=expires_at,
        ip_address=client_ip,
    )
    db.add(mfa_code)
    await db.commit()

    # Send email
    email_service = get_email_service()
    await email_service.areload_config()
    await email_service.send_mfa_code(
        to_email=user.email,
        code=raw_code,
        display_name=user.display_name,
    )

    return raw_code


def _user_avatar_base() -> Path:
    return Path(get_settings().user_assets_root)


def _build_avatar_path(user: User) -> Path | None:
    return _build_avatar_path_shared(_user_avatar_base(), user.id, user.avatar_filename)


COOKIE_NAME = "hermeshq_token"
COOKIE_MAX_AGE = 60 * 60 * 12  # 12 hours, matches access_token_minutes default


def _set_auth_cookie(response: Response, token: str) -> None:
    """Set the JWT as an httpOnly secure cookie."""
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        secure=get_settings().cookie_secure,
        samesite="lax",
        path="/",
    )


def _clear_auth_cookie(response: Response) -> None:
    """Clear the auth cookie."""
    response.set_cookie(
        key=COOKIE_NAME,
        value="",
        max_age=0,
        httponly=True,
        secure=get_settings().cookie_secure,
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


def _oidc_enabled() -> bool:
    settings = get_settings()
    return bool(settings.oidc_issuer_url and settings.oidc_client_id and settings.oidc_client_secret)


def _get_oidc_public_issuer() -> str:
    return (get_settings().oidc_issuer_url or "").rstrip("/")


def _get_oidc_discovery_base() -> str:
    settings = get_settings()
    return (settings.oidc_discovery_url or settings.oidc_issuer_url or "").rstrip("/")


def _oidc_auto_provision_enabled() -> bool:
    return bool(get_settings().oidc_auto_provision_users)


def _get_oidc_provider_login_url(provider_slug: str | None) -> str | None:
    slug = (provider_slug or "").strip().lower()
    settings = get_settings()
    if not slug:
        return None
    if slug == "google":
        return settings.oidc_provider_login_url_google
    if slug == "microsoft":
        return settings.oidc_provider_login_url_microsoft
    if slug == (settings.oidc_provider_slug or "").strip().lower():
        return None
    return None


def _get_public_oidc_provider_slugs() -> list[str]:
    """Return provider slugs from env config. Always includes google + microsoft for UI display."""
    env_slugs = [s.strip().lower() for s in (get_settings().oidc_visible_providers or "").split(",") if s.strip()]
    # Always include google + microsoft for enterprise look
    for slug in ("google", "microsoft"):
        if slug not in env_slugs:
            env_slugs.append(slug)
    return env_slugs


async def _get_db_providers(db: AsyncSession) -> list:
    """Get enabled providers from the database."""
    from hermeshq.models.oidc_provider import OidcProvider
    result = await db.execute(select(OidcProvider).where(OidcProvider.enabled.is_(True)).order_by(OidcProvider.name))
    return list(result.scalars().all())


def _get_oidc_provider_label(slug: str) -> str:
    settings = get_settings()
    normalized = slug.strip().lower()
    if normalized == (settings.oidc_provider_slug or "").strip().lower():
        return settings.oidc_provider_name or normalized.replace("-", " ").title()
    return DEFAULT_OIDC_PROVIDER_LABELS.get(normalized, normalized.replace("-", " ").title())


def _build_oidc_redirect_uri(request: Request) -> str:
    configured = get_settings().oidc_redirect_uri
    if configured:
        return configured
    return str(request.url_for("oidc_callback"))


def _build_oidc_post_logout_redirect_uri(request: Request) -> str:
    configured = get_settings().oidc_post_logout_redirect_uri
    if configured:
        return configured
    return _build_frontend_redirect(request, auth_error=None)


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
        except Exception:
            continue
    # Extract hostname from the forwarded value (may include port)
    candidate = forwarded_host.split(":")[0]
    return candidate in allowed_hosts


def _build_frontend_redirect(request: Request, *, token: str | None = None, auth_error: str | None = None) -> str:
    forwarded_host = request.headers.get("x-forwarded-host")
    if forwarded_host and not _validate_redirect_host(forwarded_host):
        logger.warning("Rejected X-Forwarded-Host %r — not in allowed origins", forwarded_host)
        forwarded_host = None
    host = forwarded_host or request.headers.get("host") or request.url.netloc
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    base_url = f"{scheme}://{host}/"
    if token:
        query = urlencode({"token": token})
        return f"{base_url}login?{query}"
    if auth_error:
        # Failed auth: redirect to /login so LoginPage.tsx can display the error
        query = urlencode({"auth_error": auth_error})
        return f"{base_url}login?{query}"
    return base_url


def _create_oidc_state() -> str:
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=OIDC_STATE_EXPIRY_MINUTES)
    return jwt.encode(
        {"nonce": secrets.token_urlsafe(16), "exp": expires_at},
        get_settings().jwt_secret,
        algorithm=get_settings().jwt_algorithm,
    )


def _validate_oidc_state(state: str | None) -> bool:
    if not state:
        return False
    try:
        jwt.decode(state, get_settings().jwt_secret, algorithms=[get_settings().jwt_algorithm])
    except JWTError:
        return False
    return True


async def _fetch_oidc_discovery() -> dict:
    discovery_base = _get_oidc_discovery_base()
    if not discovery_base:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="OIDC issuer is not configured")
    url = f"{discovery_base}/.well-known/openid-configuration"
    client = _get_http_client()
    response = await client.get(url)
    response.raise_for_status()
    return response.json()


def _translate_oidc_browser_endpoint(url: str | None) -> str | None:
    if not url:
        return url
    public_base = _get_oidc_public_issuer()
    discovery_base = _get_oidc_discovery_base()
    if public_base and discovery_base and public_base != discovery_base:
        public_parts = urlparse(public_base)
        discovery_parts = urlparse(discovery_base)
        target_parts = urlparse(url)
        if (
            target_parts.scheme == discovery_parts.scheme
            and target_parts.netloc == discovery_parts.netloc
        ):
            return urlunparse(
                (
                    public_parts.scheme or target_parts.scheme,
                    public_parts.netloc or target_parts.netloc,
                    target_parts.path,
                    target_parts.params,
                    target_parts.query,
                    target_parts.fragment,
                )
            )
    return url


def _extract_claim(data, *names: str):
    for name in names:
        if isinstance(data, dict) and data.get(name) is not None:
            return data.get(name)
    return None


def _normalize_email(email: str | None) -> str | None:
    if not email:
        return None
    normalized = email.strip().lower()
    return normalized or None


def _derive_display_name(claims: dict) -> str:
    display_name = _extract_claim(claims, "name", "preferred_username")
    if isinstance(display_name, str) and display_name.strip():
        return display_name.strip()[:128]
    first_name = _extract_claim(claims, "given_name")
    last_name = _extract_claim(claims, "family_name")
    full_name = " ".join(part.strip() for part in [str(first_name or ""), str(last_name or "")] if part.strip()).strip()
    if full_name:
        return full_name[:128]
    email = _normalize_email(_extract_claim(claims, "email"))
    if email:
        return email.split("@", 1)[0][:128]
    subject = str(_extract_claim(claims, "sub") or "oidc-user")
    return subject[:128]


def _derive_username_seed(email: str | None, display_name: str, subject: str) -> str:
    seed = (email.split("@", 1)[0] if email else display_name or subject or "user").strip().lower()
    seed = USERNAME_SANITIZER.sub("-", seed).strip("._-")
    return (seed or "user")[:56]


async def _generate_unique_username(db: AsyncSession, seed: str) -> str:
    base = seed or "user"
    candidate = base[:64]
    suffix = 2
    while True:
        result = await db.execute(select(User.id).where(User.username == candidate))
        if result.scalar_one_or_none() is None:
            return candidate
        candidate = f"{base[:56]}-{suffix}"[:64]
        suffix += 1


async def _get_local_user_by_email(db: AsyncSession, email: str | None) -> User | None:
    if not email:
        return None
    result = await db.execute(select(User).where(func.lower(User.email) == email.lower()))
    return result.scalars().first()


async def _resolve_or_create_oidc_user(db: AsyncSession, claims: dict) -> User:
    email = _normalize_email(_extract_claim(claims, "email"))
    subject = str(_extract_claim(claims, "sub") or "").strip()
    display_name = _derive_display_name(claims)

    user: User | None = None
    if subject:
        result = await db.execute(select(User).where(User.oidc_subject == subject))
        user = result.scalars().first()
    if not user:
        user = await _get_local_user_by_email(db, email)

    if not user and not _oidc_auto_provision_enabled():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This identity is not provisioned in HermesHQ")

    if not user:
        username = await _generate_unique_username(db, _derive_username_seed(email, display_name, subject))
        user = User(
            username=username,
            email=email,
            display_name=display_name,
            password_hash=hash_password(secrets.token_urlsafe(32)),
            auth_source="oidc",
            oidc_subject=subject or None,
            role="user",
            is_active=True,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user

    if email and user.email != email:
        user.email = email
    if display_name and user.display_name != display_name:
        user.display_name = display_name
    if subject and user.oidc_subject != subject:
        user.oidc_subject = subject
    user.auth_source = "oidc"
    await db.commit()
    await db.refresh(user)
    return user


async def _fetch_jwks(jwks_uri: str) -> list[dict]:
    now = time.time()
    if _JWKS_CACHE["keys"] is not None and (now - _JWKS_CACHE["fetched_at"]) < _JWKS_CACHE_TTL:
        return _JWKS_CACHE["keys"]
    client = _get_http_client()
    response = await client.get(jwks_uri)
    response.raise_for_status()
    data = response.json()
    _JWKS_CACHE["keys"] = data.get("keys", [])
    _JWKS_CACHE["fetched_at"] = now
    return _JWKS_CACHE["keys"]


async def _extract_id_token_claims(token_response: dict) -> dict:
    """Validate and extract claims from an OIDC id_token.

    Returns {} if no id_token is present.  Raises ValueError if an id_token
    exists but cannot be validated (signature failure, bad issuer, etc.)
    so the caller can reject the authentication rather than silently
    falling back to unverified userinfo claims.
    """
    id_token = token_response.get("id_token")
    if not isinstance(id_token, str) or not id_token.strip():
        return {}
    try:
        discovery = await _fetch_oidc_discovery()
        jwks_uri = discovery.get("jwks_uri")
        if not jwks_uri:
            logger.warning("OIDC discovery missing jwks_uri; skipping id_token validation")
            return {}
        keys = await _fetch_jwks(jwks_uri)
        settings = get_settings()
        issuer = (settings.oidc_issuer_url or "").rstrip("/")
        audience = settings.oidc_client_id
        algorithms = ["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"]
        unverified_header = jwt.get_unverified_header(id_token)
        kid = unverified_header.get("kid")
        candidate_keys = [k for k in keys if k.get("kid") == kid] if kid else keys
        if not candidate_keys:
            candidate_keys = keys
        for key_data in candidate_keys:
            try:
                claims = jwt.decode(
                    id_token,
                    key_data,
                    algorithms=algorithms,
                    audience=audience,
                    issuer=issuer or None,
                )
                return claims
            except JWTError:
                continue
        raise ValueError("Could not validate id_token signature with any JWKS key")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"id_token validation failed: {exc}") from exc


@router.get("/providers", response_model=AuthProvidersResponse)
async def auth_providers(db: AsyncSession = Depends(get_db_session)) -> AuthProvidersResponse:
    auth_mode = _get_auth_mode()
    providers: list[AuthProviderRead] = []

    # Collect enabled DB providers
    db_provider_slugs: set[str] = set()
    try:
        from hermeshq.models.oidc_provider import OidcProvider
        result = await db.execute(select(OidcProvider).where(OidcProvider.enabled.is_(True)))
        for p in result.scalars().all():
            db_provider_slugs.add(p.slug)
            providers.append(
                AuthProviderRead(
                    slug=p.slug,
                    name=p.name,
                    kind="oidc",
                    enabled=True,
                )
            )
    except Exception:
        logger.debug("OIDC provider discovery failed; table may not exist yet", exc_info=True)

    # Add env-configured + always-visible providers (google, microsoft)
    for slug in _get_public_oidc_provider_slugs():
        if slug not in db_provider_slugs:
            env_url = _get_oidc_provider_login_url(slug)
            providers.append(
                AuthProviderRead(
                    slug=slug,
                    name=_get_oidc_provider_label(slug),
                    kind="oidc",
                    enabled=bool(_oidc_enabled() and env_url),
                )
            )

    oidc_active = _oidc_enabled() or len(db_provider_slugs) > 0
    return AuthProvidersResponse(
        auth_mode=auth_mode,
        local_login_enabled=True,
        oidc_enabled=oidc_active,
        providers=providers,
    )


@router.post("/login")
async def login(payload: LoginRequest, response: Response, request: Request, db: AsyncSession = Depends(get_db_session)):
    client_ip = request.client.host if request.client else "unknown"
    _check_login_rate(client_ip)
    result = await db.execute(select(User).where(User.username == payload.username))
    user = result.scalar_one_or_none()
    if not user or not verify_password(payload.password, user.password_hash):
        _record_login_attempt(client_ip)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    # Check if MFA is required
    mfa_enabled = await _is_mfa_globally_enabled(db)
    if mfa_enabled and user.email:
        # Generate MFA token and send code
        mfa_token, mfa_expires = _create_mfa_token(user.id)
        try:
            await _send_mfa_code(db, user, client_ip)
        except EmailServiceError as exc:
            logger.warning("Failed to send MFA code to %s: %s", user.email, exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="MFA is enabled but email delivery is not configured. Contact your administrator.",
            )
        return MfaRequiredResponse(
            mfa_required=True,
            mfa_token=mfa_token,
            email_mask=_mask_email(user.email),
            expires_at=mfa_expires,
        )

    # No MFA — issue full token directly
    token, expires_at = create_access_token(user.id, subject_kind="id", role=user.role or "user")
    _set_auth_cookie(response, token)
    return TokenResponse(access_token=token, expires_at=expires_at)


# ---------------------------------------------------------------------------
# MFA Verification Endpoints
# ---------------------------------------------------------------------------

@router.post("/mfa/verify")
async def verify_mfa(
    payload: MfaVerifyRequest,
    response: Response,
    db: AsyncSession = Depends(get_db_session),
):
    """Verify an MFA code and issue the full JWT on success."""
    user_id = _verify_mfa_token(payload.mfa_token)
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired MFA session.",
        )

    # Look up user
    user = await db.get(User, user_id)
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive.",
        )

    # Find the latest unused MFA code for this user
    code_hash = hashlib.sha256(payload.code.encode()).hexdigest()
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(MfaCode).where(
            MfaCode.user_id == user_id,
            MfaCode.used_at.is_(None),
        ).order_by(MfaCode.created_at.desc())
    )
    mfa_codes = list(result.scalars().all())

    if not mfa_codes:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No pending verification code. Please request a new one.",
        )

    # Reject if any pending code has been locked out due to too many failed attempts
    if any(mc.failed_attempts >= MFA_CODE_MAX_ATTEMPTS for mc in mfa_codes):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed verification attempts. Please request a new code.",
        )

    # Check all pending codes (user might have requested resend)
    matched_code = None
    for mc in mfa_codes:
        # Check expiry
        expires_at = mc.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if now > expires_at:
            continue
        if mc.code_hash == code_hash:
            matched_code = mc
            break

    if not matched_code:
        # Check if the code matches an expired one (give specific error)
        for mc in mfa_codes:
            if mc.code_hash == code_hash:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Verification code has expired. Please request a new one.",
                )
        # Increment failed attempts on all pending codes to prevent brute-force
        for mc in mfa_codes:
            mc.failed_attempts += 1
        await db.commit()
        # Lock out if any code has exceeded max attempts
        if any(mc.failed_attempts >= MFA_CODE_MAX_ATTEMPTS for mc in mfa_codes):
            for mc in mfa_codes:
                mc.used_at = now
            await db.commit()
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many failed verification attempts. Please request a new code.",
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid verification code.",
        )

    # Mark code as used
    matched_code.used_at = now
    # Invalidate all other pending codes for this user
    for mc in mfa_codes:
        if mc.id != matched_code.id and mc.used_at is None:
            mc.used_at = now
    await db.commit()

    # Issue full JWT
    token, expires_at = create_access_token(user.id, subject_kind="id", role=user.role or "user")
    _set_auth_cookie(response, token)
    return TokenResponse(access_token=token, expires_at=expires_at)


@router.post("/mfa/resend")
async def resend_mfa(
    payload: MfaResendRequest,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
):
    """Resend a new MFA code to the user's email."""
    user_id = _verify_mfa_token(payload.mfa_token)
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired MFA session.",
        )

    user = await db.get(User, user_id)
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive.",
        )

    if not user.email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User has no email address configured.",
        )

    # Rate limit resend: check if a code was created in the last 30 seconds
    cooldown_threshold = datetime.now(timezone.utc) - timedelta(seconds=MFA_RESEND_COOLDOWN_SECONDS)
    recent_result = await db.execute(
        select(MfaCode).where(
            MfaCode.user_id == user_id,
            MfaCode.created_at >= cooldown_threshold,
        ).order_by(MfaCode.created_at.desc())
    )
    recent_code = recent_result.scalar_one_or_none()
    if recent_code:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Please wait {MFA_RESEND_COOLDOWN_SECONDS} seconds before requesting a new code.",
        )

    # Generate and send new code
    client_ip = request.client.host if request.client else None
    try:
        await _send_mfa_code(db, user, client_ip)
    except EmailServiceError as exc:
        logger.warning("Failed to resend MFA code to %s: %s", user.email, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to send verification email. Please try again later.",
        )

    # Issue a fresh MFA token (extends the window)
    mfa_token, mfa_expires = _create_mfa_token(user.id)

    return MfaRequiredResponse(
        mfa_required=True,
        mfa_token=mfa_token,
        email_mask=_mask_email(user.email),
        expires_at=mfa_expires,
    )


@router.get("/mfa/status", response_model=MfaStatusResponse)
async def mfa_status(
    db: AsyncSession = Depends(get_db_session),
    _user: User = Depends(get_current_user),
) -> MfaStatusResponse:
    """Get current MFA configuration status."""
    mfa_enabled = await _is_mfa_globally_enabled(db)
    email_service = get_email_service()
    await email_service.areload_config()
    return MfaStatusResponse(
        enabled=mfa_enabled,
        email_configured=email_service.is_configured,
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(
    response: Response,
    current_user: User = Depends(get_current_user),
) -> TokenResponse:
    """Issue a fresh JWT for the currently authenticated user.

    The client calls this before the existing token expires to extend
    the session without requiring a full re-login.
    """
    token, expires_at = create_access_token(current_user.id, subject_kind="id", role=current_user.role or "user")
    _set_auth_cookie(response, token)
    return TokenResponse(access_token=token, expires_at=expires_at)


@router.get("/oidc/login", include_in_schema=False)
async def oidc_login(request: Request, provider: str | None = None, db: AsyncSession = Depends(get_db_session)) -> RedirectResponse:
    requested_provider = (provider or "").strip().lower()

    # --- Try DB-based multi-provider first ---
    if requested_provider:
        try:
            from hermeshq.services import oidc_provider as oidc_svc
            db_provider = await oidc_svc.get_provider_by_slug(db, requested_provider)
            if db_provider:
                redirect_uri = _build_oidc_redirect_uri(request)
                state = oidc_svc.create_oidc_state(requested_provider, get_settings().jwt_secret)
                auth_url = await oidc_svc.build_authorization_url(db_provider, redirect_uri, state)
                return RedirectResponse(url=auth_url, status_code=status.HTTP_302_FOUND)
        except Exception:
            logger.debug("DB-backed OIDC login failed; falling through to env-based flow", exc_info=True)

    # --- Legacy env-based OIDC flow ---
    auth_mode = _get_auth_mode()
    if auth_mode == AUTH_MODE_LOCAL or not _oidc_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Enterprise authentication is not enabled")

    configured_generic_slug = ((get_settings().oidc_provider_slug or "").strip().lower() or "generic")
    allowed_providers = set(_get_public_oidc_provider_slugs()) | {configured_generic_slug}
    if requested_provider and requested_provider not in allowed_providers:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"OIDC provider '{requested_provider}' is not enabled")

    provider_login_url = _get_oidc_provider_login_url(requested_provider)
    if requested_provider and provider_login_url:
        return RedirectResponse(url=provider_login_url, status_code=status.HTTP_302_FOUND)

    if requested_provider and requested_provider != configured_generic_slug:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"OIDC provider '{requested_provider}' is not configured",
        )

    discovery = await _fetch_oidc_discovery()
    authorization_endpoint = _translate_oidc_browser_endpoint(discovery.get("authorization_endpoint"))
    if not authorization_endpoint:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="OIDC discovery missing authorization endpoint")
    params = urlencode(
        {
            "client_id": get_settings().oidc_client_id,
            "redirect_uri": _build_oidc_redirect_uri(request),
            "response_type": "code",
            "scope": get_settings().oidc_scope,
            "state": _create_oidc_state(),
        }
    )
    return RedirectResponse(url=f"{authorization_endpoint}?{params}", status_code=status.HTTP_302_FOUND)


@router.get("/oidc/logout", include_in_schema=False)
async def oidc_logout(
    request: Request,
    provider: str | None = None,
    db: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    # Clear auth cookie
    resp_kwargs = {"auth_error": None}

    # --- DB-provider social logout ---
    if provider:
        try:
            from hermeshq.services import oidc_provider as oidc_svc
            db_provider = await oidc_svc.get_provider_by_slug(db, provider)
            if db_provider:
                social_url = oidc_svc.get_logout_url(db_provider, _build_oidc_post_logout_redirect_uri(request))
                if social_url:
                    redirect = RedirectResponse(url=social_url, status_code=status.HTTP_302_FOUND)
                    _clear_auth_cookie(redirect)
                    return redirect
        except Exception:
            logger.debug("OIDC logout failed; falling through to local logout", exc_info=True)

    # --- Legacy env-based logout ---
    if not _oidc_enabled():
        redirect = RedirectResponse(_build_frontend_redirect(request, **resp_kwargs), status_code=status.HTTP_302_FOUND)
        _clear_auth_cookie(redirect)
        return redirect
    try:
        discovery = await _fetch_oidc_discovery()
        end_session_endpoint = _translate_oidc_browser_endpoint(discovery.get("end_session_endpoint"))
        if not end_session_endpoint:
            redirect = RedirectResponse(_build_frontend_redirect(request, **resp_kwargs), status_code=status.HTTP_302_FOUND)
            _clear_auth_cookie(redirect)
            return redirect
        params = urlencode(
            {
                "post_logout_redirect_uri": _build_oidc_post_logout_redirect_uri(request),
            }
        )
        redirect = RedirectResponse(url=f"{end_session_endpoint}?{params}", status_code=status.HTTP_302_FOUND)
        _clear_auth_cookie(redirect)
        return redirect
    except Exception:  # noqa: BLE001
        redirect = RedirectResponse(_build_frontend_redirect(request, **resp_kwargs), status_code=status.HTTP_302_FOUND)
        _clear_auth_cookie(redirect)
        return redirect


@router.get("/oidc/callback", name="oidc_callback", include_in_schema=False)
async def oidc_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    db: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    if error:
        return RedirectResponse(
            _build_frontend_redirect(request, auth_error=(error_description or error)),
            status_code=status.HTTP_302_FOUND,
        )
    if not code:
        return RedirectResponse(
            _build_frontend_redirect(request, auth_error="Missing OIDC authorization code"),
            status_code=status.HTTP_302_FOUND,
        )

    # --- Try DB-provider state first (includes provider slug) ---
    from hermeshq.services import oidc_provider as oidc_svc
    state_payload = oidc_svc.verify_oidc_state(state, get_settings().jwt_secret)
    local_user = None

    if state_payload and state_payload.get("provider"):
        # Multi-provider (DB) flow
        try:
            provider = await oidc_svc.get_provider_by_slug(db, state_payload["provider"])
            if not provider:
                raise ValueError(f"Provider '{state_payload['provider']}' not found or disabled")
            claims = await oidc_svc.exchange_code_and_get_claims(provider, code, _build_oidc_redirect_uri(request))
            local_user = await oidc_svc.resolve_or_create_user(db, claims, provider)
        except Exception:
            logger.exception("OIDC authentication failed (DB provider flow)")
            return RedirectResponse(
                _build_frontend_redirect(request, auth_error="Enterprise authentication failed. Please try again or contact support."),
                status_code=status.HTTP_302_FOUND,
            )
    elif _validate_oidc_state(state):
        # Legacy env-based flow
        try:
            discovery = await _fetch_oidc_discovery()
            token_endpoint = discovery.get("token_endpoint")
            userinfo_endpoint = discovery.get("userinfo_endpoint")
            if not token_endpoint:
                raise ValueError("OIDC discovery missing token endpoint")
            client = _get_http_client()
            token_response = await client.post(
                token_endpoint,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": _build_oidc_redirect_uri(request),
                    "client_id": get_settings().oidc_client_id,
                    "client_secret": get_settings().oidc_client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            token_response.raise_for_status()
            token_payload = token_response.json()
            claims = await _extract_id_token_claims(token_payload)
            access_token = token_payload.get("access_token")
            if userinfo_endpoint and access_token:
                userinfo_response = await client.get(
                    userinfo_endpoint,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                userinfo_response.raise_for_status()
                claims = {**claims, **userinfo_response.json()}
            if not claims.get("sub"):
                raise ValueError("OIDC user claims did not include sub")
            local_user = await _resolve_or_create_oidc_user(db, claims)
        except Exception:
            logger.exception("OIDC authentication failed (legacy env flow)")
            return RedirectResponse(
                _build_frontend_redirect(request, auth_error="Enterprise authentication failed. Please try again or contact support."),
                status_code=status.HTTP_302_FOUND,
            )
    else:
        return RedirectResponse(
            _build_frontend_redirect(request, auth_error="Invalid OIDC login state"),
            status_code=status.HTTP_302_FOUND,
        )

    if not local_user or not local_user.is_active:
        return RedirectResponse(
            _build_frontend_redirect(request, auth_error="This HermesHQ user is inactive"),
            status_code=status.HTTP_302_FOUND,
        )
    token, _ = create_access_token(local_user.id, subject_kind="id", role=local_user.role or "user")
    redirect = RedirectResponse(_build_frontend_redirect(request, token=token), status_code=status.HTTP_302_FOUND)
    _set_auth_cookie(redirect, token)
    return redirect


# ---------------------------------------------------------------------------
# Password Reset (Resend email)
# ---------------------------------------------------------------------------

@router.post("/forgot-password", response_model=PasswordResetResponse)
async def forgot_password(
    payload: ForgotPasswordRequest,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> PasswordResetResponse:
    """Request a password reset link via email. Always returns 200 to prevent email enumeration."""
    # Find user by email (case-insensitive)
    result = await db.execute(
        select(User).where(
            func.lower(User.email) == payload.email.strip().lower(),
            User.is_active == True,  # noqa: E712
            User.auth_source == "local",
        )
    )
    user = result.scalar_one_or_none()

    if not user or not user.email:
        # Always return success to prevent enumeration
        return PasswordResetResponse(
            message="If that email is registered, a reset link has been sent."
        )

    # Rate limit: max 3 reset requests per user per hour
    one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    recent_result = await db.execute(
        select(func.count(PasswordResetToken.id)).where(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.created_at >= one_hour_ago,
        )
    )
    recent_count = recent_result.scalar() or 0
    if recent_count >= 3:
        return PasswordResetResponse(
            message="If that email is registered, a reset link has been sent."
        )

    # Generate a secure token
    raw_token = secrets.token_urlsafe(48)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    settings = get_settings()
    expires_minutes = settings.password_reset_token_minutes or 15
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=expires_minutes)

    # Get client IP
    forwarded = request.headers.get("X-Forwarded-For", "")
    ip = forwarded.split(",")[0].strip() if forwarded else request.client.host if request.client else None

    reset_token = PasswordResetToken(
        user_id=user.id,
        token_hash=token_hash,
        expires_at=expires_at,
        ip_address=ip,
    )
    db.add(reset_token)
    await db.commit()

    # Send email
    email_service = get_email_service()
    await email_service.areload_config()
    try:
        await email_service.send_password_reset(
            to_email=user.email,
            token=raw_token,
            display_name=user.display_name,
        )
    except EmailServiceError as exc:
        logger.warning("Failed to send password reset email to %s: %s", user.email, exc)
        # Don't reveal the error to the client

    return PasswordResetResponse(
        message="If that email is registered, a reset link has been sent."
    )


@router.post("/reset-password", response_model=PasswordResetResponse)
async def reset_password(
    payload: ResetPasswordRequest,
    db: AsyncSession = Depends(get_db_session),
) -> PasswordResetResponse:
    """Reset password using a valid reset token."""
    # Hash the provided token to find it
    token_hash = hashlib.sha256(payload.token.encode()).hexdigest()

    result = await db.execute(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == token_hash,
            PasswordResetToken.used_at.is_(None),
        )
    )
    reset_token = result.scalar_one_or_none()

    if not reset_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired reset token.",
        )

    # Check expiration
    now = datetime.now(timezone.utc)
    if reset_token.expires_at.tzinfo is None:
        reset_token.expires_at = reset_token.expires_at.replace(tzinfo=timezone.utc)
    if now > reset_token.expires_at:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Reset token has expired. Please request a new one.",
        )

    # Get user
    user = await db.get(User, reset_token.user_id)
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired reset token.",
        )

    # Update password
    user.password_hash = hash_password(payload.new_password)

    # Mark token as used
    reset_token.used_at = now

    # Invalidate any other pending reset tokens for this user
    other_result = await db.execute(
        select(PasswordResetToken).where(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.id != reset_token.id,
        )
    )
    for other_token in other_result.scalars().all():
        other_token.used_at = now

    await db.commit()

    logger.info("Password reset successful for user %s", user.username)

    return PasswordResetResponse(
        message="Password has been reset successfully."
    )


@router.get("/email-config", response_model=EmailConfigStatus)
async def get_email_config(
    _admin: User = Depends(get_current_user),
) -> EmailConfigStatus:
    """Get current email configuration status (admin only)."""
    email_service = get_email_service()
    await email_service.areload_config()
    return EmailConfigStatus(
        configured=email_service.is_configured,
        from_email=email_service._from_email,
        from_name=email_service._from_name,
        public_base_url=email_service._public_base_url,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(response: Response, current_user: User = Depends(get_current_user)) -> Response:
    _clear_auth_cookie(response)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/me", response_model=UserRead)
async def me(request: Request, current_user: User = Depends(get_current_user)) -> UserRead:
    return _serialize_user(request, current_user)


@router.put("/me/preferences", response_model=UserRead)
async def update_preferences(
    payload: UserPreferencesUpdate,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> UserRead:
    if payload.theme_preference is not None:
        if payload.theme_preference not in {"default", "dark", "light", "system", "enterprise", "sixmanager", "sixmanager-light"}:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid theme preference")
        current_user.theme_preference = payload.theme_preference
    if payload.locale_preference is not None:
        if payload.locale_preference not in {"default", "en", "es"}:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid locale preference")
        current_user.locale_preference = payload.locale_preference
    await db.commit()
    await db.refresh(current_user)
    return _serialize_user(request, current_user)


@router.put("/me/profile", response_model=UserRead)
async def update_profile(
    payload: UserProfileUpdate,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> UserRead:
    current_user.display_name = payload.display_name
    await db.commit()
    await db.refresh(current_user)
    return _serialize_user(request, current_user)


@router.put("/me/password", status_code=status.HTTP_204_NO_CONTENT)
async def update_my_password(
    payload: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
):
    if not verify_password(payload.current_password, current_user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect")
    if payload.current_password == payload.new_password:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="New password must be different")
    current_user.password_hash = hash_password(payload.new_password)
    await db.commit()


@router.get("/me/avatar", include_in_schema=False)
async def get_my_avatar(current_user: User = Depends(get_current_user)):
    if not current_user.avatar_filename:
        raise HTTPException(status_code=404, detail="Avatar not found")
    avatar_path = _build_avatar_path(current_user)
    if not avatar_path or not avatar_path.exists():
        raise HTTPException(status_code=404, detail="Avatar not found")
    return FileResponse(avatar_path, media_type=resolve_media_type(avatar_path))


@router.post("/me/avatar", response_model=UserRead)
async def upload_my_avatar(
    request: Request,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> UserRead:
    current_user.avatar_filename = await validate_and_save_avatar(_user_avatar_base(), current_user.id, file)
    await db.commit()
    await db.refresh(current_user)
    return _serialize_user(request, current_user)


@router.delete("/me/avatar", response_model=UserRead)
async def delete_my_avatar(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> UserRead:
    _delete_avatar_files_shared(_user_avatar_base(), current_user.id)
    current_user.avatar_filename = None
    await db.commit()
    await db.refresh(current_user)
    return _serialize_user(request, current_user)
