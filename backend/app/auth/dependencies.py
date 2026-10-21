import uuid
from typing import Annotated

from fastapi import Depends, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import sessions
from app.auth.models import User
from app.core.config import settings
from app.core.database import get_db
from app.core.exceptions import ApiError
from app.core.redis import get_redis
from app.core.security import decode_token


async def get_current_user(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> User:
    token = request.cookies.get("accessToken")
    if not token:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:]
    if not token:
        raise ApiError(401, "Unauthorized request")

    payload = decode_token(token, settings.access_token_secret)
    user_id, sid = payload.get("sub"), payload.get("sid")
    if not user_id or not sid:
        raise ApiError(401, "Invalid token")

    if not await sessions.session_exists(redis, user_id, sid):
        raise ApiError(401, "Session ended. Please log in again.")

    user = await db.get(User, uuid.UUID(user_id))
    if user is None:
        raise ApiError(401, "Invalid token")
    return user