from datetime import datetime , timezone
from redis.asyncio import Redis

from app.core.config import settings
from app.core.security import hash_token

def refresh_key(user_id: str, sid: str) -> str:
    return f"refresh:{user_id}:{sid}"


def sessions_key(user_id: str) -> str:
    return f"sessions:{user_id}"

async def create_session(
    redis: Redis,
    *,
    user_id: str,
    sid: str,
    refresh_token: str,
    user_agent: str | None,
    ip: str | None,
) -> None:
    ttl = settings.refresh_token_expire_days * 24 * 60 * 60
    key = refresh_key(user_id, sid)

    pipe = redis.pipeline()
    pipe.hset(
        key,
        mapping={
            "token_hash": hash_token(refresh_token),
            "user_agent": user_agent or "",
            "ip": ip or "",
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    pipe.expire(key, ttl)
    pipe.sadd(sessions_key(user_id), sid)
    pipe.expire(sessions_key(user_id), ttl)
    await pipe.execute()

#  rotation logic of the token 

ROTATE_OK, ROTATE_MISSING, ROTATE_REUSED = 1, 0, -1

# Atomic: only swap the hash if the stored one matches the token being replaced.
_ROTATE_SCRIPT = """
local current = redis.call('HGET', KEYS[1], 'token_hash')
if not current then return 0 end
if current ~= ARGV[1] then return -1 end
redis.call('HSET', KEYS[1], 'token_hash', ARGV[2])
redis.call('EXPIRE', KEYS[1], ARGV[3])
return 1
"""


async def session_exists(redis: Redis, user_id: str, sid: str) -> bool:
    return bool(await redis.exists(refresh_key(user_id, sid)))


async def rotate_session(
    redis: Redis, *, user_id: str, sid: str, old_token: str, new_token: str
) -> int:
    ttl = settings.refresh_token_expire_days * 24 * 60 * 60
    result = await redis.eval(
        _ROTATE_SCRIPT, 1, refresh_key(user_id, sid),
        hash_token(old_token), hash_token(new_token), ttl,
    )
    if result == ROTATE_OK:
        await redis.expire(sessions_key(user_id), ttl)
    return int(result)


async def revoke_session(redis: Redis, user_id: str, sid: str) -> None:
    pipe = redis.pipeline()
    pipe.delete(refresh_key(user_id, sid))
    pipe.srem(sessions_key(user_id), sid)
    await pipe.execute()


async def revoke_all_sessions(redis: Redis, user_id: str) -> None:
    sids = await redis.smembers(sessions_key(user_id))
    pipe = redis.pipeline()
    for sid in sids:
        pipe.delete(refresh_key(user_id, sid))
    pipe.delete(sessions_key(user_id))
    await pipe.execute()