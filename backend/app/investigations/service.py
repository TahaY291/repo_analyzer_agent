import logging
import time
import uuid
from datetime import datetime, timezone

from langchain_core.messages import AIMessage
from sqlalchemy import func, select

from app.agent.github_client import GitHubError, GitHubUnavailable, RateLimited, RepoNotFound
from app.agent.graph import make_initial_state, merge_update, run_config
from app.agent.llm import LLMBusy
from app.core.config import settings
from app.core.database import SessionLocal
from app.investigations.models import Investigation, InvestigationStatus, TraceStep

log = logging.getLogger(__name__)
OUTPUT_PREVIEW_CHARS = 1500

def _last_tool_call(messages) -> dict | None:
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and msg.tool_calls:
            return msg.tool_calls[0]
    return None

def _describe(node: str, changes: dict, last_call: dict | None) -> dict:
    """Turn one node's update into the columns of a TraceStep row."""
    if node == "fetch_tree":
        total = changes["metadata"].get("total_files", len(changes["file_tree"]))
        return {"thought": f"Fetched the repository tree ({total} files)"}

    if node == "choose_tool":
        msgs = changes.get("messages")
        if msgs and msgs[0].tool_calls:
            call = msgs[0].tool_calls[0]
            return {"thought": f"Chose {call['name']}", "tool_called": call["name"], "tool_input": call["args"]}
        return {"thought": f"The model could not pick a tool ({changes.get('stop_reason')})", "is_error": True}

    if node == "tools":
        output = str(changes["messages"][0].content)
        return {
            "tool_called": last_call["name"] if last_call else None,
            "tool_input": last_call["args"] if last_call else None,
            "tool_output": output[:OUTPUT_PREVIEW_CHARS],
            "output_chars": len(output),
            "is_error": output.startswith("ERROR"),
        }

    if node == "evaluate":
        findings = changes.get("findings") or []
        thought = "; ".join(f"[{f['relevance']}] {f['file']}: {f['summary']}" for f in findings)
        thought = thought or "Nothing to summarize from this step"
        if changes.get("stop_reason"):
            thought += f" | stopping: {changes['stop_reason']}"
        return {"thought": thought}

    if node == "write_report":
        return {"thought": "Wrote the final report"}

    return {}


    
async def run_investigation(investigation_id: uuid.UUID, graph, resume: bool = False) -> None:
    async with SessionLocal() as db:
        inv = await db.get(Investigation, investigation_id)
        if inv is None:
            log.error("Investigation %s not found", investigation_id)
            return

        repo_url, owner, repo, focus = inv.repo_url, inv.owner, inv.repo, inv.focus
        inv.status = InvestigationStatus.RUNNING.value
        inv.started_at = inv.started_at or datetime.now(timezone.utc)
        inv.finished_at = None
        inv.error = None
        inv.model = settings.groq_model
        await db.commit()

        # The thread_id is what ties every checkpoint to this investigation.
        # recursion_limit only covers THIS call, but step and token limits live in the state, so they persist.
        thread = {**run_config(), "configurable": {"thread_id": str(investigation_id)}}
        error: str | None = None
        seq = 0
        try:
            if resume:
                snapshot = await graph.aget_state(thread)
                state = dict(snapshot.values)
                graph_input = None   # None = continue from the saved checkpoint
                seq = (await db.execute(
                    select(func.max(TraceStep.step)).where(TraceStep.investigation_id == investigation_id)
                )).scalar() or 0
                seq += 1
                db.add(TraceStep(
                    investigation_id=investigation_id, step=seq, node="resume",
                    thought=f"Resumed from the last checkpoint (next: {', '.join(snapshot.next) or 'nothing'})",
                ))
                await db.commit()
            else:
                state = make_initial_state(str(investigation_id), repo_url, owner, repo, focus)
                graph_input = state

            last_call = _last_tool_call(state["messages"])
            t_prev = time.perf_counter()

            async for update in graph.astream(graph_input, config=thread, stream_mode="updates"):
                for node, changes in update.items():
                    now = time.perf_counter()
                    before_in, before_out = state["tokens_in"], state["tokens_out"]
                    state = merge_update(state, changes)

                    row = _describe(node, changes, last_call)
                    if node == "choose_tool" and changes.get("messages") and changes["messages"][0].tool_calls:
                        last_call = changes["messages"][0].tool_calls[0]

                    seq += 1
                    db.add(TraceStep(
                        investigation_id=investigation_id, step=seq, node=node,
                        duration_ms=int((now - t_prev) * 1000),
                        tokens_in=(state["tokens_in"] - before_in) or None,
                        tokens_out=(state["tokens_out"] - before_out) or None,
                        **row,
                    ))
                    inv.step_count = state["step"]
                    inv.tokens_in = state["tokens_in"]
                    inv.tokens_out = state["tokens_out"]
                    await db.commit()
                    t_prev = time.perf_counter()

            if state["report"] is None:
                raise RuntimeError("The agent finished without a report")

            inv.report = state["report"]
            inv.findings = state["findings"]
            inv.stop_reason = state["stop_reason"]
            inv.status = InvestigationStatus.COMPLETED.value

        except RateLimited:
            error = "GitHub rate limit reached. You can resume this investigation in a few minutes."
        except LLMBusy as e:
            error = f"{e} You can resume this investigation."
        except (RepoNotFound, GitHubUnavailable, GitHubError) as e:
            error = str(e)[:500]
        except Exception:
            log.exception("Investigation %s crashed", investigation_id)
            error = "Something went wrong while investigating this repository."

        if error:
            inv.status = InvestigationStatus.FAILED.value
            inv.error = error
            db.add(TraceStep(investigation_id=investigation_id, step=seq + 1, node="error", thought=error, is_error=True))

        inv.finished_at = datetime.now(timezone.utc)
        await db.commit()