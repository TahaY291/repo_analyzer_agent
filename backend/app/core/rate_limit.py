from fastapi import Request
from app.core.exceptions import ApiError
from app.core.redis import redis_client


def rate_limit(name: str , limit: int , window_seconds: int):
    """Returns a dependency that allows `limit` requests per ip per window"""

    async def dependency(request: Request) ->None:
        ip = request.client.host if request.client else "Unknown"
        key = f"rl:{name}:{ip}"

        pipe  = redis_client.pipeline()
        pipe.set(key, 0 , ex=window_seconds , nx=True)
        pipe.incr(key)
        _, count = await pipe.execute()

        if count > limit: 
            raise ApiError(429, "Too many attempts please try again latter.")

    return dependency
    