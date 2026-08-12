from datetime import UTC, datetime, timedelta

from fastapi import Cookie, Depends, HTTPException, WebSocket, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.config import get_settings
from hermeshq.database import get_db_session
from hermeshq.models.agent import Agent
from hermeshq.models.agent_assignment import AgentAssignment
from hermeshq.models.user import User

settings = get_settings()
pwd_context = CryptContext(schemes=["argon2", "pbkdf2_sha256"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)
ROLE_ADMIN = "admin"
ROLE_USER = "user"


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password, password_hash)


def create_access_token(
    subject: str,
    *,
    subject_kind: str = "id",
    role: str = "user",
    agent_ids: list[str] | None = None,
) -> tuple[str, datetime]:
    expires_at = datetime.now(UTC) + timedelta(minutes=settings.access_token_minutes)
    payload: dict = {"sub": subject, "sub_kind": subject_kind, "role": role, "exp": expires_at}
    if agent_ids is not None:
        payload["agent_ids"] = agent_ids
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, expires_at


def decode_access_token(token: str) -> str | None:
    subject, _ = decode_access_token_subject(token)
    return subject


def decode_access_token_subject(token: str) -> tuple[str | None, str | None]:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError:
        return None, None
    return payload.get("sub"), payload.get("sub_kind")


def decode_access_token_claims(token: str) -> dict | None:
    """Decode JWT and return all claims without DB lookup. Returns None if invalid."""
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError:
        return None


def create_agent_service_token(agent_id: str, token_version: int = 1) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": agent_id,
            "sub_kind": "agent",
            "token_version": token_version,
            "iat": now,
            "exp": now + timedelta(days=settings.agent_service_token_days),
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )


def verify_agent_service_token(token: str, agent_id: str, token_version: int = 1) -> bool:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError:
        return False
    return (
        payload.get("sub") == agent_id
        and payload.get("sub_kind") == "agent"
        and payload.get("token_version") == token_version
    )


async def get_user_by_username(db: AsyncSession, username: str | None) -> User | None:
    if not username:
        return None
    result = await db.execute(select(User).where(User.username == username))
    return result.scalar_one_or_none()


async def get_user_by_subject(db: AsyncSession, subject: str | None, subject_kind: str | None = None) -> User | None:
    if not subject:
        return None
    if subject_kind == "id":
        return await db.get(User, subject)
    if subject_kind == "username":
        return await get_user_by_username(db, subject)
    user = await db.get(User, subject)
    if user:
        return user
    result = await db.execute(
        select(User).where(
            or_(
                User.username == subject,
                User.oidc_subject == subject,
            )
        )
    )
    return result.scalar_one_or_none()


async def _resolve_token_from_request(
    bearer_token: str | None = None,
    cookie_token: str | None = None,
) -> str:
    """Return the first available token from Authorization header or cookie."""
    return bearer_token or cookie_token or ""


async def get_current_user(
    bearer_token: str | None = Depends(oauth2_scheme),
    cookie_token: str | None = Cookie(default=None, alias="hermeshq_token"),
    db: AsyncSession = Depends(get_db_session),
) -> User:
    token = bearer_token or cookie_token or ""
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid authentication credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    subject, subject_kind = decode_access_token_subject(token)
    if not subject:
        raise credentials_error
    user = await get_user_by_subject(db, subject, subject_kind)
    if not user or not user.is_active:
        raise credentials_error
    return user


async def get_websocket_user(websocket: WebSocket, db: AsyncSession, token: str | None = None) -> User | None:
    token = token or websocket.cookies.get("hermeshq_token")
    subject, subject_kind = decode_access_token_subject(token or "")
    user = await get_user_by_subject(db, subject, subject_kind)
    if not user or not user.is_active:
        return None
    return user


def is_admin(user: User) -> bool:
    return (user.role or ROLE_USER) == ROLE_ADMIN


async def require_admin(current_user: User = Depends(get_current_user)) -> User:
    if not is_admin(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return current_user


async def get_accessible_agent_ids(db: AsyncSession, user: User) -> set[str]:
    if is_admin(user):
        result = await db.execute(select(Agent.id))
        return set(result.scalars().all())
    result = await db.execute(select(AgentAssignment.agent_id).where(AgentAssignment.user_id == user.id))
    return set(result.scalars().all())


async def can_access_agent(db: AsyncSession, user: User, agent_id: str) -> bool:
    if is_admin(user):
        return bool(await db.get(Agent, agent_id))
    result = await db.execute(
        select(AgentAssignment.id).where(
            AgentAssignment.user_id == user.id,
            AgentAssignment.agent_id == agent_id,
        )
    )
    return result.scalar_one_or_none() is not None


async def ensure_agent_access(db: AsyncSession, user: User, agent_id: str) -> Agent:
    agent = await db.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    if await can_access_agent(db, user, agent_id):
        return agent
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Agent access denied")
