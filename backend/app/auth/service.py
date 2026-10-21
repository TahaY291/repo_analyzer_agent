import secrets

from fastapi import BackgroundTasks
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import User
from app.core.email import send_email
from app.core.exceptions import ApiError
from app.core.security import hash_password
import uuid
from app.core.config import settings
from app.core.security import decode_token

from app.auth import sessions
from app.core.security import (
    DUMMY_PASSWORD_HASH,
    create_access_token,
    create_refresh_token,
    hash_password,
    verify_password,
)

MAX_LOGIN_ATTEMPTS = 5
LOGIN_ATTEMPT_WINDOW_SECONDS = 15 * 60

OTP_TTL_SECONDS = 10 * 60


def verify_otp_key(email: str) -> str:
    return f"otp:verify:{email}"


def reset_otp_key(email: str) -> str:
    return f"otp:reset:{email}"


def generate_otp() -> str:
    return str(secrets.randbelow(900_000) + 100_000)   # always 6 digits


async def _send_otp_email(to: str, otp: str) -> None:
    try:
        await send_email(
            to=to,
            subject="Verify your email",
            text=f"Your verification code is {otp}. It expires in 10 minutes.",
        )
    except Exception as e:  # runs after the response, so nobody else would see this
        print(f"Failed to send OTP email to {to}: {e}")

VERIFY_ATTEMPTS_LIMIT = 5
VERIFY_ATTEMPTS_WINDOW_SECONDS = 15 * 60
RESEND_COOLDOWN_SECONDS = 60


def verify_attempts_key(email: str) -> str:
    return f"verify:attempts:{email}"


def resend_cooldown_key(email: str) -> str:
    return f"otp:resend:cooldown:{email}"


async def issue_verification_otp(
    redis: Redis, background_tasks: BackgroundTasks, email: str
) -> None:
    otp = generate_otp()
    await redis.set(verify_otp_key(email), otp, ex=OTP_TTL_SECONDS)
    await redis.delete(verify_attempts_key(email))   # fresh code, fresh guesses
    background_tasks.add_task(_send_otp_email, email, otp)

async def register_user(
        
    db: AsyncSession,
    redis: Redis,
    background_tasks: BackgroundTasks,
    *,
    name: str,
    email: str,
    password: str,
) -> User:
    email = email.strip().lower()

    result = await db.execute(select(User.id).where(User.email == email))
    if result.scalar_one_or_none() is not None:
        raise ApiError(400, "User with this email already exists")

    user = User(
        name=name.strip(),
        email=email,
        password_hash=await hash_password(password),
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError:          # two requests raced past the check above
        await db.rollback()
        raise ApiError(400, "User with this email already exists")
    await db.refresh(user)          # load DB-generated values like created_at

    await issue_verification_otp(redis, background_tasks, user.email)

    return user


# to create a session or start session for both routes login user and verify email route
async def _start_session(
    redis: Redis, user: User, *, user_agent: str | None, ip: str | None
) -> tuple[str, str]:
    sid = uuid.uuid4().hex
    access_token = create_access_token(str(user.id), user.email, sid)
    refresh_token = create_refresh_token(str(user.id), sid)
    await sessions.create_session(
        redis,
        user_id=str(user.id),
        sid=sid,
        refresh_token=refresh_token,
        user_agent=user_agent,
        ip=ip,
    )
    return access_token, refresh_token

async def login_user(
    db: AsyncSession,
    redis: Redis,
    *,
    email: str,
    password: str,
    user_agent: str | None,
    ip: str | None,
) -> tuple[User, str, str]:
    email = email.strip().lower()
    attempts_key = f"login:attempts:{email}"

    # 1. locked out? (checked before touching the password)
    attempts = await redis.get(attempts_key)
    if attempts is not None and int(attempts) >= MAX_LOGIN_ATTEMPTS:
        raise ApiError(429, "Too many failed attempts. Please try again later.")

    # 2. find the user and check the password (always one bcrypt run)
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    hash_to_check = user.password_hash if user else DUMMY_PASSWORD_HASH
    password_ok = await verify_password(password, hash_to_check)

    if user is None or not password_ok:
        pipe = redis.pipeline()
        pipe.set(attempts_key, 0, ex=LOGIN_ATTEMPT_WINDOW_SECONDS, nx=True)
        pipe.incr(attempts_key)
        await pipe.execute()
        raise ApiError(401, "Invalid email or password")

    await redis.delete(attempts_key)

    # 3. only now reveal that the account isn't verified
    if not user.is_verified:
        raise ApiError(403, "Please verify your email first")

    # 4. create tokens and the session
    access_token, refresh_token = await _start_session(redis, user, user_agent=user_agent, ip=ip)
    return user, access_token, refresh_token

async def logout_user(redis: Redis, refresh_token: str | None) -> None:
    if not refresh_token:
        return
    try:
        payload = decode_token(refresh_token, settings.refresh_token_secret)
    except ApiError:
        return  # expired or invalid: nothing left to revoke
    user_id, sid = payload.get("sub"), payload.get("sid")
    if user_id and sid:
        await sessions.revoke_session(redis, user_id, sid)


async def refresh_session(
    db: AsyncSession, redis: Redis, *, refresh_token: str
) -> tuple[str, str]:
    payload = decode_token(refresh_token, settings.refresh_token_secret)
    user_id, sid = payload.get("sub"), payload.get("sid")
    if not user_id or not sid:
        raise ApiError(401, "Invalid refresh token")

    user = await db.get(User, uuid.UUID(user_id))
    if user is None:
        await sessions.revoke_all_sessions(redis, user_id)
        raise ApiError(401, "Invalid refresh token")

    new_refresh = create_refresh_token(user_id, sid)
    result = await sessions.rotate_session(
        redis, user_id=user_id, sid=sid, old_token=refresh_token, new_token=new_refresh
    )
    if result == sessions.ROTATE_MISSING:
        raise ApiError(401, "Session expired. Please log in again.")
    if result == sessions.ROTATE_REUSED:
        await sessions.revoke_all_sessions(redis, user_id)
        raise ApiError(401, "Refresh token reuse detected. Please log in again.")

    return create_access_token(user_id, user.email, sid), new_refresh

async def verify_email(
    db: AsyncSession,
    redis: Redis,
    *,
    email: str,
    otp: str,
    user_agent: str | None,
    ip: str | None,
) -> tuple[User, str, str]:
    email = email.strip().lower()

    # 1. count this attempt (the counter is created with its TTL, then incremented)
    attempts_key = verify_attempts_key(email)
    pipe = redis.pipeline()
    pipe.set(attempts_key, 0, ex=VERIFY_ATTEMPTS_WINDOW_SECONDS, nx=True)
    pipe.incr(attempts_key)
    _, attempts = await pipe.execute()

    if attempts > VERIFY_ATTEMPTS_LIMIT:
        await redis.delete(verify_otp_key(email))   # burn the code: no more guessing
        raise ApiError(429, "Too many attempts. Please request a new OTP.")

    # 2. check the OTP
    stored = await redis.get(verify_otp_key(email))
    if stored is None or not secrets.compare_digest(stored, otp):
        raise ApiError(400, "Invalid or expired OTP")

    # 3. mark the user verified
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if user is None:
        raise ApiError(400, "Invalid or expired OTP")

    user.is_verified = True      # change an attribute on a loaded object...
    await db.commit()            # ...and the session writes the UPDATE
    await db.refresh(user)       # reload (updated_at is set by the database)

    # 4. clean up and log the user in
    await redis.delete(verify_otp_key(email), attempts_key)
    access_token, refresh_token = await _start_session(redis, user, user_agent=user_agent, ip=ip)
    return user, access_token, refresh_token

async def resend_verification_otp(
    db: AsyncSession,
    redis: Redis,
    background_tasks: BackgroundTasks,
    *,
    email: str,
) -> None:
    email = email.strip().lower()

    # Claim the cooldown atomically: set only if absent. Returns None if already set.
    claimed = await redis.set(resend_cooldown_key(email), "1", ex=RESEND_COOLDOWN_SECONDS, nx=True)
    if not claimed:
        raise ApiError(429, "Please wait before requesting another OTP")

    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    # Silent if the user is missing or already verified (same response either way)
    if user is not None and not user.is_verified:
        await issue_verification_otp(redis, background_tasks, user.email)