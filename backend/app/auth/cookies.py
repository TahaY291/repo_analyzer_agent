from fastapi import Response

from app.core.config import settings


def set_auth_cookies(response: Response, access_token: str, refresh_token: str) -> None:
    common = {
        "httponly": True,
        "secure": settings.cookie_secure,
        "samesite": settings.cookie_samesite,
    }
    response.set_cookie(
        "accessToken",
        access_token,
        max_age=settings.access_token_expire_minutes * 60,
        **common,
    )
    response.set_cookie(
        "refreshToken",
        refresh_token,
        max_age=settings.refresh_token_expire_days * 24 * 60 * 60,
        path="/api/v1/auth",   # the browser only sends it to auth routes
        **common,
    )


def clear_auth_cookies(response: Response) -> None:
    common = {
        "httponly": True,
        "secure": settings.cookie_secure,
        "samesite": settings.cookie_samesite,
    }
    response.delete_cookie("accessToken", path="/", **common)
    response.delete_cookie("refreshToken", path="/api/v1/auth", **common)