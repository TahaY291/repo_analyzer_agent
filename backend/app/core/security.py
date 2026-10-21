import asyncio
import hashlib
from datetime import datetime , timedelta , timezone

import bcrypt
import jwt
import uuid
from app.core.config import settings
from app.core.exceptions import ApiError

ALGORITHM = 'HS256'
# Compared against when the email doesn't exist, so response time doesn't reveal it.
DUMMY_PASSWORD_HASH = bcrypt.hashpw(b"dummy-password", bcrypt.gensalt(rounds=10)).decode()

def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()

async def hash_password(password: str) -> str:
    hashed  =  await asyncio.to_thread(bcrypt.hashpw, password.encode(), bcrypt.gensalt(rounds=10))
    return hashed.decode()

async def verify_password(plain: str, hashed:str) ->bool:
    return await asyncio.to_thread(bcrypt.checkpw, plain.encode(), hashed.encode())

def _encode(payload: dict, secret: str, expires: timedelta) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode({**payload, "iat": now, "exp": now + expires}, secret, algorithm=ALGORITHM)

def create_access_token(user_id: str, email: str, session_id: str) -> str:
    return _encode(
        {"sub": user_id, "email": email, "sid": session_id},
        settings.access_token_secret,
        timedelta(minutes=settings.access_token_expire_minutes),
    )


def create_refresh_token(user_id: str, session_id: str) -> str:
    return _encode(
        {"sub": user_id, "sid": session_id, "jti": uuid.uuid4().hex},
        settings.refresh_token_secret,
        timedelta(days=settings.refresh_token_expire_days),
    )

def decode_token(token: str, secret: str) -> dict:
    try:
        return jwt.decode(token, secret, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise ApiError(401, "Token expired")
    except jwt.InvalidTokenError:
        raise ApiError(401, "Invalid token")