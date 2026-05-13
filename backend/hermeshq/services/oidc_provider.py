"""OIDC service — handles multi-provider OIDC flows with discovery caching."""

import logging
import time
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.models.oidc_provider import OidcProvider
from hermeshq.models.user import User

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Discovery + JWKS cache
# ---------------------------------------------------------------------------
_CACHE_TTL = 3600  # 1 hour
_discovery_cache: dict[str, dict] = {}
_jwks_cache: dict[str, dict] = {}


async def _fetch_discovery(discovery_url: str) -> dict:
    """Fetch and cache OIDC discovery document."""
    now = time.time()
    cached = _discovery_cache.get(discovery_url)
    if cached and (now - cached["_fetched_at"]) < _CACHE_TTL:
        return cached
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(f"{discovery_url.rstrip('/')}/.well-known/openid-configuration")
        resp.raise_for_status()
        doc = resp.json()
    doc["_fetched_at"] = now
    _discovery_cache[discovery_url] = doc
    return doc


async def _fetch_jwks(jwks_uri: str) -> list[dict]:
    """Fetch and cache JWKS keys."""
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
    """Create a signed state token that includes the provider slug."""
    import json, base64
    payload = {
        "provider": provider_slug,
        "nonce": secrets.token_urlsafe(16),
        "exp": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
    }
    raw = json.dumps(payload).encode()
    sig = jwt.encode({"data": base64.b64encode(raw).decode()}, jwt_secret, algorithm="HS256")
    return sig


def verify_oidc_state(state: str, jwt_secret: str) -> dict | None:
    """Verify state token and return the payload dict (with 'provider')."""
    try:
        decoded = jwt.decode(state, jwt_secret, algorithms=["HS256"])
        import base64, json
        raw = base64.b64decode(decoded["data"])
        return json.loads(raw)
    except Exception:
        logger.warning("Invalid OIDC state token", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Authorization URL builder
# ---------------------------------------------------------------------------
async def build_authorization_url(
    provider: OidcProvider,
    redirect_uri: str,
    state: str,
) -> str:
    """Build the full authorization URL for a given OIDC provider."""
    discovery = await _fetch_discovery(provider.discovery_url)
    auth_endpoint = discovery.get("authorization_endpoint")
    if not auth_endpoint:
        raise HTTPException(status_code=502, detail="OIDC discovery missing authorization_endpoint")
    params = urlencode({
        "client_id": provider.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": provider.scopes,
        "state": state,
    })
    return f"{auth_endpoint}?{params}"


# ---------------------------------------------------------------------------
# Code exchange + token validation
# ---------------------------------------------------------------------------
async def exchange_code_and_get_claims(
    provider: OidcProvider,
    code: str,
    redirect_uri: str,
) -> dict:
    """Exchange auth code for tokens, validate id_token, fetch userinfo."""
    discovery = await _fetch_discovery(provider.discovery_url)
    token_endpoint = discovery.get("token_endpoint")
    if not token_endpoint:
        raise HTTPException(status_code=502, detail="OIDC discovery missing token_endpoint")

    async with httpx.AsyncClient(timeout=15.0) as client:
        # Exchange code for tokens
        token_resp = await client.post(
            token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": provider.client_id,
                "client_secret": provider.client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        token_resp.raise_for_status()
        token_payload = token_resp.json()

        claims: dict = {}

        # Validate id_token if present
        id_token = token_payload.get("id_token")
        if id_token and isinstance(id_token, str):
            claims = await _validate_id_token(id_token, provider, discovery)

        # Fetch userinfo for additional claims
        access_token = token_payload.get("access_token")
        userinfo_endpoint = discovery.get("userinfo_endpoint")
        if userinfo_endpoint and access_token:
            try:
                ui_resp = await client.get(
                    userinfo_endpoint,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                ui_resp.raise_for_status()
                claims = {**claims, **ui_resp.json()}
            except Exception:
                logger.warning("Failed to fetch userinfo from %s", provider.slug, exc_info=True)

        if not claims.get("sub"):
            raise ValueError("OIDC claims did not include 'sub'")

        return claims


async def _validate_id_token(id_token: str, provider: OidcProvider, discovery: dict) -> dict:
    """Validate id_token signature using provider's JWKS."""
    jwks_uri = discovery.get("jwks_uri")
    if not jwks_uri:
        logger.warning("No jwks_uri in discovery for %s; skipping validation", provider.slug)
        try:
            return jwt.decode(id_token, options={"verify_signature": False})
        except Exception:
            return {}

    keys = await _fetch_jwks(jwks_uri)
    if not keys:
        logger.warning("No JWKS keys for %s", provider.slug)
        return {}

    for key_data in keys:
        try:
            return jwt.decode(
                id_token,
                key=key_data,
                algorithms=["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"],
                audience=provider.client_id,
                issuer=discovery.get("issuer"),
                options={"verify_exp": True},
            )
        except jwt.ExpiredSignatureError:
            raise
        except Exception:
            continue
    logger.warning("Could not validate id_token for provider %s", provider.slug)
    return {}


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

    # Check allowed domains
    if provider.allowed_domains:
        allowed = [d.strip().lower() for d in provider.allowed_domains.split(",") if d.strip()]
        if allowed and email:
            domain = email.split("@")[-1]
            if domain not in allowed:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Email domain '{domain}' is not allowed. Allowed: {', '.join(allowed)}",
                )

    # Find by OIDC subject
    user: User | None = None
    if subject:
        result = await db.execute(
            select(User).where(User.oidc_subject == f"{provider.slug}:{subject}")
        )
        user = result.scalar_one_or_none()

    # Find by email
    if not user and email:
        result = await db.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()

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
            oidc_subject=f"{provider.slug}:{subject}" if subject else None,
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
    if subject:
        composite = f"{provider.slug}:{subject}"
        if user.oidc_subject != composite:
            user.oidc_subject = composite
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
