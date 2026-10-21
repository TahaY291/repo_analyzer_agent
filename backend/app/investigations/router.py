from uuid import UUID
from sqlalchemy import select
from app.investigations.models import TraceStep
import re
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from fastapi import Request
from sqlalchemy import update
from app.investigations.models import Investigation
from app.investigations.service import run_investigation
from app.core.database import get_db
from app.auth.dependencies import get_current_user

router = APIRouter()

_GITHUB_URL = re.compile(r"^https?://(?:www\.)?github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?(?:/.*)?$")


def parse_github_url(url: str) -> tuple[str, str]:
    match = _GITHUB_URL.match(url.strip())
    if not match:
        raise HTTPException(422, "Enter a valid GitHub repository URL")
    return match.group(1), match.group(2)


class InvestigationCreate(BaseModel):
    repo_url: str
    focus: str | None = Field(default=None, max_length=500)


@router.post("/investigations", status_code=202)
async def create_investigation(
    body: InvestigationCreate,
    request: Request, 
    background_tasks: BackgroundTasks,
    db=Depends(get_db),                # your async session dependency
    user=Depends(get_current_user),    # your auth dependency
):
    owner, repo = parse_github_url(body.repo_url)
    inv = Investigation(
        user_id=user.id,
        repo_url=body.repo_url.strip(),
        owner=owner,
        repo=repo,
        focus=(body.focus or "").strip() or None,
    )
    db.add(inv)
    await db.commit()
    await db.refresh(inv)

    background_tasks.add_task(run_investigation, inv.id , request.app.state.agent_graph)   # runs after the response is sent
    return {"id": str(inv.id), "status": inv.status}

async def _get_owned(db, investigation_id: UUID, user) -> Investigation:
    inv = await db.get(Investigation, investigation_id)
    if inv is None or inv.user_id != user.id:
        raise HTTPException(404, "Investigation not found")   # same answer for "not yours", so ids don't leak
    return inv


@router.get("/investigations")
async def list_investigations(limit: int = 20, db=Depends(get_db), user=Depends(get_current_user)):
    result = await db.execute(
        select(Investigation)
        .where(Investigation.user_id == user.id)
        .order_by(Investigation.created_at.desc())
        .limit(min(limit, 50))
    )
    return [
        {"id": str(i.id), "repo_url": i.repo_url, "focus": i.focus, "status": i.status, "created_at": i.created_at}
        for i in result.scalars().all()
    ]


@router.get("/investigations/{investigation_id}")
async def get_investigation(investigation_id: UUID, db=Depends(get_db), user=Depends(get_current_user)):
    inv = await _get_owned(db, investigation_id, user)
    return {
        "id": str(inv.id), "repo_url": inv.repo_url, "owner": inv.owner, "repo": inv.repo, "focus": inv.focus,
        "status": inv.status, "stop_reason": inv.stop_reason, "error": inv.error,
        "step_count": inv.step_count, "tokens_in": inv.tokens_in, "tokens_out": inv.tokens_out,
        "report": inv.report,
        "created_at": inv.created_at, "started_at": inv.started_at, "finished_at": inv.finished_at,
    }


@router.get("/investigations/{investigation_id}/trace")
async def get_trace(investigation_id: UUID, after: int = 0, db=Depends(get_db), user=Depends(get_current_user)):
    await _get_owned(db, investigation_id, user)
    result = await db.execute(
        select(TraceStep)
        .where(TraceStep.investigation_id == investigation_id, TraceStep.step > after)
        .order_by(TraceStep.step)
    )
    return [
        {
            "step": s.step, "node": s.node, "thought": s.thought, "tool_called": s.tool_called,
            "tool_input": s.tool_input, "tool_output": s.tool_output, "is_error": s.is_error,
            "duration_ms": s.duration_ms, "tokens_in": s.tokens_in, "tokens_out": s.tokens_out,
            "created_at": s.created_at,
        }
        for s in result.scalars().all()
    ]

@router.post("/investigations/{investigation_id}/resume", status_code=202)
async def resume_investigation(
    investigation_id: UUID,
    request: Request,
    background_tasks: BackgroundTasks,
    db=Depends(get_db),
    user=Depends(get_current_user),
):
    await _get_owned(db, investigation_id, user)          # 404 if it is missing or not yours
    graph = request.app.state.agent_graph

    snapshot = await graph.aget_state({"configurable": {"thread_id": str(investigation_id)}})
    if not snapshot.next:
        raise HTTPException(409, "Nothing to resume: this investigation has no saved progress left")

    # Claim it with one atomic UPDATE: if two requests arrive, only one matches the WHERE.
    claimed = await db.execute(
        update(Investigation)
        .where(Investigation.id == investigation_id, Investigation.status.in_(["interrupted", "failed"]))
        .values(status="pending", error=None)
        .returning(Investigation.id)
    )
    if claimed.scalar_one_or_none() is None:
        await db.rollback()
        # Don't touch any ORM object here: the rollback expired them, and reading one raises MissingGreenlet
        raise HTTPException(409, "This investigation is already being resumed, or cannot be resumed")
    await db.commit()

    background_tasks.add_task(run_investigation, investigation_id, graph, True)
    return {"id": str(investigation_id), "status": "pending"}