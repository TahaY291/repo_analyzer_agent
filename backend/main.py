import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import update
from starlette.middleware.trustedhost import TrustedHostMiddleware
from app.agent.checkpoint import open_checkpointer
from app.agent.graph import build_graph

from app.agent.github_client import github_client
from app.auth.router import router as auth_router
from app.core.config import settings
from app.core.database import SessionLocal, engine
from app.core.exceptions import register_exception_handlers
from app.core.redis import redis_client
from app.core.request_context import RequestContextMiddleware
from app.core.security_headers import SecurityHeadersMiddleware
from app.investigations.models import Investigation
from app.investigations.router import router as agent_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await redis_client.ping()
    log.info("Connected to Redis")

    async with open_checkpointer() as saver:
        app.state.agent_graph = build_graph(saver)

        # A restart kills background tasks. Mark them interrupted: they can now be resumed.
        try:
            async with SessionLocal() as db:
                await db.execute(
                    update(Investigation)
                    .where(Investigation.status.in_(["pending", "running"]))
                    .values(status="interrupted", error="The server stopped while this was running. You can resume it.")
                )
                await db.commit()
        except Exception:
            log.exception("Could not mark unfinished investigations")

        yield

    await github_client.aclose()
    await redis_client.aclose()
    await engine.dispose()

app = FastAPI(
    title="GitHub Repository Intelligence Agent",
    version="0.1.0",
    lifespan=lifespan,
)

register_exception_handlers(app)
app.add_middleware(RequestContextMiddleware)   # added first, so it ends up innermost
app.add_middleware(SecurityHeadersMiddleware, hsts=settings.environment == "production")
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
app.include_router(auth_router, prefix="/api/v1")
app.include_router(agent_router, prefix="/api/v1")


@app.get("/health")
async def health():
    return {"status": "ok"}