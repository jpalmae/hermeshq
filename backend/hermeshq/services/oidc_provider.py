"""OIDC service — handles multi-provider OIDC flows with discovery caching."""

import hmac
import logging
import secrets
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import HTTPException, status
from jwt.exceptions import InvalidTokenError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.models.oidc_provider import OidcProvider
from hermeshq.models.user import User

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Discovery + JWKS cache
# ---------------------------------------------------------------------------
_CACHE_TTL = 3600  # 1 hour
_MAX_CACHE_ENTRIES = 50
_discovery_cache: dict[str, dict] = {}
_jwks_cache: dict[str, dict] = {}


def _evict_cache(cache: dict[str, dict]) -> None:
    """Remove expired entries and trim to max size."""
    now = time.time()
    # Remove expired
    expired = [k for k, v in cache.items() if (now - v.get("_fetched_at", 0)) >= _CACHE_TTL]
    for k in expired:
        cache.pop(k, None)
    # Trim to max size (remove oldest first)
    while len(cache) > _MAX_CACHE_ENTRIES:
        oldest_key = min(cache, key=lambda k: cache[k].get("_fetched_at", 0), default=None)
        if oldest_key:
            cache.pop(oldest_key)
        else:
            break


def _discovery_endpoint(discovery_url: str) -> str:
    """Return the well-known URL, avoiding duplication if already present."""
    base = discovery_url.rstrip("/")
    suffix = "/.well-known/openid-configuration"
    if base.endswith("/.well-known/openid-configuration"):
        return base
    return base + suffix


async def _fetch_discovery(discovery_url: str) -> dict:
    """Fetch and cache OIDC discovery document."""
    _evict_cache(_discovery_cache)
    now = time.time()
    cached = _discovery_cache.get(discovery_url)
    if cached and (now - cached["_fetched_at"]) < _CACHE_TTL:
        return cached
    url = _discovery_endpoint(discovery_url)
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        doc = resp.json()
    doc["_fetched_at"] = now
    _discovery_cache[discovery_url] = doc
    return doc


async def _fetch_jwks(jwks_uri: str) -> list[dict]:
    """Fetch and cache JWKS keys."""
    _evict_cache(_jwks_cache)
    now = time.time()
    cached = _jwks_cache.get(jwks_uri)
    if cached and (now - cached["_fetched_at"]) < _CACHE_TTL:
        return cached["keys"]
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(jwks_uri)
        resp.raise_for_status()
        data = resp.json()
    _jwks_cache[jwks_uri] = {"keys": data.get("keys", []), "_fetched_at": now}
    return data.get("keys", [])


# ---------------------------------------------------------------------------
# Provider resolution
# ---------------------------------------------------------------------------
async def get_provider_by_slug(db: AsyncSession, slug: str) -> OidcProvider | None:
    result = await db.execute(select(OidcProvider).where(OidcProvider.slug == slug, OidcProvider.enabled.is_(True)))
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# OIDC state management (includes provider slug)
# ---------------------------------------------------------------------------
def create_oidc_state(provider_slug: str, jwt_secret: str) -> str:
    payload = {
        "provider": provider_slug,
        "nonce": secrets.token_urlsafe(16),
    }
    exp = datetime.now(UTC) + timedelta(minutes=10)
    return jwt.encode(
        {**payload, "exp": exp},
        jwt_secret,
        algorithm="HS256",
    )


def verify_oidc_state(state: str | None, jwt_secret: str) -> dict | None:
    if not state:
        return None
    try:
        decoded = jwt.decode(state, jwt_secret, algorithms=["HS256"])
        if not decoded.get("provider") or not decoded.get("nonce"):
            return None
        return decoded
    except (InvalidTokenError, KeyError, ValueError, TypeError):
        logger.warning("Invalid OIDC state token", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Authorization URL builder
# ---------------------------------------------------------------------------
async def build_authorization_url(
    provider: OidcProvider,
    redirect_uri: str,
    state: str,
    nonce: str,
) -> str:
    """Build the full authorization URL for a given OIDC provider."""
    discovery = await _fetch_discovery(provider.discovery_url)
    auth_endpoint = discovery.get("authorization_endpoint")
    if not auth_endpoint:
        raise HTTPException(status_code=502, detail="OIDC discovery missing authorization_endpoint")
    params = urlencode(
        {
            "client_id": provider.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": provider.scopes,
            "state": state,
            "nonce": nonce,
        }
    )
    return f"{auth_endpoint}?{params}"


# ---------------------------------------------------------------------------
# Code exchange + token validation
# ---------------------------------------------------------------------------
async def exchange_code_and_get_claims(
    provider: OidcProvider,
    code: str,
    redirect_uri: str,
    expected_nonce: str,
) -> dict:
    """Exchange auth code for tokens, validate id_token, fetch userinfo."""
    discovery = await _fetch_discovery(provider.discovery_url)
    token_endpoint = discovery.get("token_endpoint")
    if not token_endpoint:
        raise HTTPException(status_code=502, detail="OIDC discovery missing token_endpoint")

    from hermeshq.config import get_settings
    from hermeshq.services.secret_vault import build_vault_from_settings, decrypt_value

    client_secret = decrypt_value(build_vault_from_settings(get_settings()), provider.client_secret)
    if not client_secret:
        raise ValueError(f"OIDC client secret for '{provider.slug}' cannot be decrypted")

    async with httpx.AsyncClient(timeout=15.0) as client:
        # Exchange code for tokens
        token_resp = await client.post(
            token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": provider.client_id,
                "client_secret": client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        token_resp.raise_for_status()
        token_payload = token_resp.json()

        logger.debug("OIDC token_payload keys: %s", list(token_payload.keys()))

        id_token = token_payload.get("id_token")
        if not id_token or not isinstance(id_token, str):
            raise ValueError("OIDC token response did not include an id_token")
        claims = await _validate_id_token(id_token, provider, discovery, expected_nonce)
        logger.debug("OIDC id_token claims keys: %s", list(claims.keys()))

        # Fetch userinfo for additional claims
        access_token = token_payload.get("access_token")
        userinfo_endpoint = discovery.get("userinfo_endpoint")
        logger.debug("OIDC userinfo_endpoint: %s, has access_token: %s", userinfo_endpoint, bool(access_token))
        if userinfo_endpoint and access_token:
            try:
                ui_resp = await client.get(
                    userinfo_endpoint,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                ui_resp.raise_for_status()
                userinfo = ui_resp.json()
                logger.debug("OIDC userinfo keys: %s", list(userinfo.keys()))
                if userinfo.get("sub") != claims.get("sub"):
                    raise ValueError("OIDC userinfo subject does not match id_token subject")
                claims = {**userinfo, **claims}
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                logger.warning("Failed to fetch OIDC userinfo from %s: %s", provider.slug, exc, exc_info=True)
        else:
            logger.debug("OIDC skipping userinfo: endpoint=%s, token=%s", bool(userinfo_endpoint), bool(access_token))

        logger.debug("OIDC final claims keys: %s", list(claims.keys()))

        if not claims.get("sub"):
            raise ValueError("OIDC claims did not include 'sub'")

        return claims


async def _validate_id_token(
    id_token: str,
    provider: OidcProvider,
    discovery: dict,
    expected_nonce: str,
) -> dict:
    """Validate id_token signature using provider's JWKS."""
    jwks_uri = discovery.get("jwks_uri")
    if not jwks_uri:
        raise ValueError(f"OIDC provider '{provider.slug}' discovery is missing jwks_uri")

    keys = await _fetch_jwks(jwks_uri)
    if not keys:
        raise ValueError(f"OIDC provider '{provider.slug}' returned no JWKS keys")

    for key_data in keys:
        try:
            kty = str(key_data.get("kty", "")).upper()
            if kty == "RSA":
                public_key = jwt.algorithms.RSAAlgorithm.from_jwk(key_data)
            elif kty == "EC":
                public_key = jwt.algorithms.ECAlgorithm.from_jwk(key_data)
            else:
                continue
            claims = jwt.decode(
                id_token,
                key=public_key,
                algorithms=["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"],
                audience=provider.client_id,
                issuer=discovery.get("issuer"),
                options={"verify_exp": True},
            )
            if not hmac.compare_digest(str(claims.get("nonce") or ""), expected_nonce):
                raise ValueError("OIDC nonce mismatch")
            return claims
        except jwt.ExpiredSignatureError:
            raise
        except InvalidTokenError:
            continue
    raise ValueError(f"Could not validate id_token for provider '{provider.slug}'")


# ---------------------------------------------------------------------------
# User resolution / auto-provision
# ---------------------------------------------------------------------------
async def resolve_or_create_user(
    db: AsyncSession,
    claims: dict,
    provider: OidcProvider,
) -> User:
    """Find existing user or create a new one based on OIDC claims."""
    from hermeshq.core.security import hash_password

    subject = str(claims.get("sub", "")).strip()
    email = (claims.get("email") or "").strip().lower()
    display_name = claims.get("name") or claims.get("display_name") or email.split("@")[0]
    email_verified = claims.get("email_verified") is True or str(claims.get("email_verified", "")).lower() == "true"

    if not subject:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="OIDC identity has no subject")
    if email and not email_verified:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="OIDC email address is not verified")

    # Check allowed domains
    if provider.allowed_domains:
        allowed = [d.strip().lower() for d in provider.allowed_domains.split(",") if d.strip()]
        if allowed and not email:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="A verified email address is required")
        if allowed and email:
            domain = email.split("@")[-1]
            if domain not in allowed:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Email domain '{domain}' is not allowed. Allowed: {', '.join(allowed)}",
                )

    composite_subject = f"{provider.slug}:{subject}"
    user: User | None = None
    result = await db.execute(select(User).where(User.oidc_subject == composite_subject))
    user = result.scalar_one_or_none()

    # Find by email
    if not user and email:
        result = await db.execute(select(User).where(User.email == email))
        email_user = result.scalar_one_or_none()
        if (
            email_user
            and email_user.auth_source == "oidc"
            and not email_user.oidc_subject
            and email_user.role != "admin"
        ):
            user = email_user

    # Auto-provision
    if not user and not provider.auto_provision:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This identity is not provisioned in HermesHQ",
        )

    if not user:
        import re

        base = re.sub(r"[^a-z0-9_.-]", "", display_name.lower().replace(" ", "_"))[:32] or "user"
        username = base
        counter = 1
        while True:
            check = await db.execute(select(User).where(User.username == username))
            if not check.scalar_one_or_none():
                break
            username = f"{base}_{counter}"
            counter += 1

        user = User(
            username=username,
            email=email,
            display_name=display_name,
            password_hash=hash_password(secrets.token_urlsafe(32)),
            auth_source="oidc",
            oidc_subject=composite_subject,
            role="user",
            is_active=True,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user

    # Update existing user
    if email and user.email != email:
        user.email = email
    if display_name and user.display_name != display_name:
        user.display_name = display_name
    if user.oidc_subject != composite_subject:
        user.oidc_subject = composite_subject
    user.auth_source = "oidc"
    await db.commit()
    await db.refresh(user)
    return user


# ---------------------------------------------------------------------------
# Social logout URLs
# ---------------------------------------------------------------------------
def get_logout_url(provider: OidcProvider, post_logout_uri: str) -> str | None:
    """Build a logout URL that also signs out from the social provider."""
    slug = provider.slug.lower()
    if slug == "google":
        return f"https://accounts.google.com/Logout?continue={post_logout_uri}"
    discovery_url = provider.discovery_url.lower()
    if "microsoftonline" in discovery_url or slug == "microsoft":
        # Extract tenant from discovery URL
        parts = provider.discovery_url.split("/")
        for i, p in enumerate(parts):
            if "microsoftonline" in p and i + 1 < len(parts):
                tenant = parts[i + 1]
                return f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/logout?post_logout_redirect_uri={post_logout_uri}"
    return None
