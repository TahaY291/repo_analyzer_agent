from contextlib import asynccontextmanager
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.core.config import settings


def psycopg_url(url: str) -> str:
    """Your app URL is for asyncpg. psycopg wants a plain postgresql:// URL and sslmode."""
    parts = urlsplit(url.replace("postgresql+asyncpg://", "postgresql://", 1))
    query = dict(parse_qsl(parts.query))
    if "ssl" in query:
        query["sslmode"] = query.pop("ssl")
    return urlunsplit(parts._replace(query=urlencode(query)))


@asynccontextmanager
async def open_checkpointer():
    """Open the checkpoint store. To switch to Redis later, change only this function."""
    pool = AsyncConnectionPool(
        conninfo=psycopg_url(settings.database_url),
        max_size=5,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        check=AsyncConnectionPool.check_connection,   # replaces connections that Neon closed while idle
        open=False,
    )
    await pool.open()
    try:
        saver = AsyncPostgresSaver(pool)
        await saver.setup()   # creates the checkpoint tables on first run
        yield saver
    finally:
        await pool.close()