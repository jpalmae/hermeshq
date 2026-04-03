from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Query, WebSocket, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.config import get_settings
from hermeshq.database import get_db_session
from hermeshq.models.agent import Agent
from hermeshq.models.agent_assignment import AgentAssignment
from hermeshq.models.user import User

settings = get_settings()
pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")
ROLE_ADMIN = "admin"
ROLE_USER = "user"


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password, password_hash)


def create_access_token(subject: str) -> tuple[str, datetime]:
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_minutes)
    token = jwt.encode(
        {"sub": subject, "exp": expires_at},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    return token, expires_at


def decode_access_token(token: str) -> str | None:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError:
        return None
    return payload.get("sub")


async def get_user_by_username(db: AsyncSession, username: str | None) -> User | None:
    if not username:
        return None
    result = await db.execute(select(User).where(User.username == username))
    return result.scalar_one_or_none()


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db_session),
) -> User:
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid authentication credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    username = decode_access_token(token)
    if not username:
        raise credentials_error
    user = await get_user_by_username(db, username)
    if not user or not user.is_active:
        raise credentials_error
    return user


async def get_current_user_from_query_token(
    token: str = Query(...),
    db: AsyncSession = Depends(get_db_session),
) -> User:
    username = decode_access_token(token)
    if not username:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication credentials")
    user = await get_user_by_username(db, username)
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication credentials")
    return user


async def get_websocket_user(websocket: WebSocket, db: AsyncSession) -> User | None:
    token = websocket.query_params.get("token")
    username = decode_access_token(token or "")
    user = await get_user_by_username(db, username)
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
