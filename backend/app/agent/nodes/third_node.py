from typing import Literal

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from pydantic import BaseModel, Field

from app.agent.llm import ainvoke_json
from app.agent.prompts import EVALUATE_PROMPT
from app.agent.state import Finding, InvestigationState
from app.core.config import settings


class FindingOut(BaseModel):
    file: str = Field(description="Exact path of the file, as shown in the tool result")
    summary: str = Field(
        description="2-4 sentences: what this part of the file does, the key names in it "
        "(functions, classes, routes, config), and what it tells us about the focus"
    )
    relevance: Literal["high", "medium", "low"]


class Evaluation(BaseModel):
    findings: list[FindingOut] = Field(description="One entry per file read in the last step")
    decision: Literal["read_more", "done"] = Field(
        description="'done' only if the findings already answer the focus well"
    )
    next_hint: str | None = Field(
        default=None,
        description="If read_more: what to look at next (file, offset, or a name to search for). One sentence.",
    )


def _format_findings(findings: list[Finding]) -> str:
    if not findings:
        return "(none yet)"
    return "\n".join(f"- [{f['relevance']}] {f['file']}: {f['summary']}" for f in findings)


def _last_round(messages) -> tuple[list[dict], dict[str, ToolMessage]]:
    """The latest AI message with tool calls, plus the tool results that came after it."""
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if isinstance(msg, AIMessage) and msg.tool_calls:
            results = {m.tool_call_id: m for m in messages[i + 1 :] if isinstance(m, ToolMessage)}
            return msg.tool_calls, results
    return [], {}


def _as_offset(value) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


async def evaluate_progress(state: InvestigationState) -> dict:
    """Node 3. LLM: turn the file part just read into a finding, then decide read_more or done.

    Limits are plain Python, so a confused LLM can never loop forever.
    """
    tool_calls, tool_msgs = _last_round(state["messages"])

    blocks: list[str] = []
    read_ok: list[tuple[str, int, ToolMessage]] = []   # (path, offset, tool message)
    for call in tool_calls:
        msg = tool_msgs.get(call["id"])
        output = str(msg.content) if msg else "(no result)"
        blocks.append(f"TOOL: {call['name']} {call['args']}\nRESULT:\n{output}")
        if call["name"] == "read_repository_file" and msg and not output.startswith("ERROR"):
            read_ok.append((call["args"].get("file_path", ""), _as_offset(call["args"].get("offset")), msg))

    offset_by_path = {path: off for path, off, _ in read_ok}
    tokens_in, tokens_out = state["tokens_in"], state["tokens_out"]
    new_findings: list[Finding] = []
    shrunk: list[ToolMessage] = []
    decision, hint = "read_more", None

    # Only call the LLM if a file part was actually read. A directory listing, a search
    # or an error has nothing to summarize, so we skip the call and save tokens.
    if read_ok:
        system = SystemMessage(
            content=EVALUATE_PROMPT.format(
                focus=state["focus"] or "No specific focus. Give a general overview of the repo.",
                findings=_format_findings(state["findings"]),
                files_read="\n".join(state["files_read"]) or "(none yet)",
                steps_left=max(settings.max_agent_steps - state["step"], 0),
            )
        )
        human = HumanMessage("LAST STEP RESULTS:\n\n" + "\n\n".join(blocks))
        result = await ainvoke_json([system, human], Evaluation)

        raw = result["raw"]
        usage = (raw.usage_metadata if raw else None) or {}
        tokens_in += usage.get("input_tokens", 0)
        tokens_out += usage.get("output_tokens", 0)

        parsed: Evaluation | None = result["parsed"]
        if parsed is not None:
            new_findings = [
                Finding(file=f.file, summary=f.summary, relevance=f.relevance)
                for f in parsed.findings
                if f.file in offset_by_path   # ignore findings about files we never read
            ]
            decision, hint = parsed.decision, parsed.next_hint

            # Replace the big file text with a short stub. The stub keeps the "FILE: ... (characters a-b
            # of N; more remains, continue with offset=...)" header, so node 2 still knows what part was read.
            kept = {f["file"] for f in new_findings}
            shrunk = [
                ToolMessage(
                    content=f"[{str(msg.content).splitlines()[0]} was read. Its summary is in FINDINGS.]",
                    tool_call_id=msg.tool_call_id,
                    id=msg.id,   # same id: add_messages replaces the old message
                    name=msg.name,
                )
                for path, _, msg in read_ok
                if path in kept
            ]

    # Hard limits first, the LLM's opinion last
    if state["step"] >= settings.max_agent_steps:
        stop_reason = "step_limit"
    elif tokens_in + tokens_out >= settings.max_tokens_per_investigation:
        stop_reason = "token_budget"
    elif decision == "done":
        stop_reason = "enough_evidence"
    else:
        stop_reason = None

    # A file counts as read ONLY if it produced a finding.
    new_paths = [p for p in dict.fromkeys(f["file"] for f in new_findings) if p not in state["files_read"]]
    return {
        "findings": new_findings,                      # operator.add: only the NEW ones
        "files_read": new_paths,
        "chunks_read": [f"{f['file']}@{offset_by_path[f['file']]}" for f in new_findings],
        "messages": shrunk,
        "next_hint": hint,
        "stop_reason": stop_reason,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
    }


def route_after_evaluate(state: InvestigationState) -> str:
    return "write_report" if state["stop_reason"] else "choose_tool"