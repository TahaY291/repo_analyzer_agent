from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response

from app.auth.cookies import set_auth_cookies
from app.auth.schemas import LoginIn, LoginOut, RegisterIn, UserOut , VerifyEmailIn , ResendOtpIn
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession
from app.auth.cookies import clear_auth_cookies, set_auth_cookies
from app.auth.dependencies import get_current_user
from app.auth.models import User
from app.core.exceptions import ApiError

from app.auth import service
from app.core.database import get_db
from app.core.rate_limit import rate_limit
from app.core.redis import get_redis
from app.core.responses import ApiResponse

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register",
    status_code=201,
    response_model=ApiResponse[UserOut],
    dependencies=[Depends(rate_limit("register", limit=4, window_seconds=3600))],
)
async def register(
    body: RegisterIn,
    background_tasks: BackgroundTasks,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
):
    user = await service.register_user(
        db,
        redis,
        background_tasks,
        name=body.name,
        email=body.email,
        password=body.password,
    )
    return ApiResponse(
        data=UserOut.model_validate(user),
        message="User registered successfully. Please check your email for the OTP.",
    )

@router.post(
    "/login",
    response_model=ApiResponse[UserOut],
    dependencies=[Depends(rate_limit("login", limit=20, window_seconds=900))],
)
async def login(
    body: LoginIn,
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
):
    user, access_token, refresh_token = await service.login_user(
        db,
        redis,
        email=body.email,
        password=body.password,
        user_agent=request.headers.get("user-agent"),
        ip=request.client.host if request.client else None,
    )
    set_auth_cookies(response, access_token, refresh_token)
    return ApiResponse(
        data=UserOut.model_validate(user),
        message="User logged in successfully",
    )

@router.post(
    "/refresh",
    response_model=ApiResponse[None],
    dependencies=[Depends(rate_limit("refresh", limit=60, window_seconds=900))],
)
async def refresh(
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
):
    token = request.cookies.get("refreshToken")
    if not token:
        raise ApiError(401, "Unauthorized - refresh token missing")

    access_token, refresh_token = await service.refresh_session(db, redis, refresh_token=token)
    set_auth_cookies(response, access_token, refresh_token)
    return ApiResponse(message="Tokens refreshed successfully")


@router.post("/logout", response_model=ApiResponse[None])
async def logout(
    request: Request,
    response: Response,
    redis: Annotated[Redis, Depends(get_redis)],):
    await service.logout_user(redis, request.cookies.get("refreshToken"))
    clear_auth_cookies(response)
    return ApiResponse(message="User logged out successfully")


@router.get("/me", response_model=ApiResponse[UserOut])
async def me(user: Annotated[User, Depends(get_current_user)]):
    return ApiResponse(data=UserOut.model_validate(user))

@router.post(
    "/verify-email",
    response_model=ApiResponse[UserOut],
    dependencies=[Depends(rate_limit("verify-email", limit=20, window_seconds=900))],
)
async def verify_email(
    body: VerifyEmailIn,
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
):
    user, access_token, refresh_token = await service.verify_email(
        db,
        redis,
        email=body.email,
        otp=body.otp,
        user_agent=request.headers.get("user-agent"),
        ip=request.client.host if request.client else None,
    )
    set_auth_cookies(response, access_token, refresh_token)
    return ApiResponse(data=UserOut.model_validate(user), message="Email verified successfully")


@router.post(
    "/resend-otp",
    response_model=ApiResponse[None],
    dependencies=[Depends(rate_limit("resend-otp", limit=10, window_seconds=3600))],
)
async def resend_otp(
    body: ResendOtpIn,
    background_tasks: BackgroundTasks,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
):
    await service.resend_verification_otp(db, redis, background_tasks, email=body.email)
    return ApiResponse(message="If your account needs verification, a new OTP has been sent.")