from functools import lru_cache

from langchain_core.messages import HumanMessage, SystemMessage

from app.agent.llm import ainvoke_tool_call, get_llm
from app.agent.prompts import SELECT_TOOL_PROMPT
from app.agent.state import Finding, InvestigationState
from app.agent.tools import TOOLS
from app.agent.tree_view import list_directory_entries
from app.core.config import settings


@lru_cache
def _llm_with_tools():
    """Build the Groq client once and give it ALL tools from tools.py."""
    return get_llm().bind_tools(TOOLS, tool_choice="any")


def _format_findings(findings: list[Finding]) -> str:
    if not findings:
        return "(none yet)"
    return "\n".join(f"- [{f['relevance']}] {f['file']}: {f['summary']}" for f in findings)


async def select_next_tool(state: InvestigationState) -> dict:
    """Node 2. LLM: reads focus, repo info and findings, then picks the next tool call.

    It does NOT run the tool. It only returns the AI message that contains the tool call.
    ToolNode runs it, and node 3 (evaluate) reads the result.
    """
    meta = state["metadata"]
    top_level = list_directory_entries(state["file_tree"], "", settings.max_listing_entries) or "(empty)"

    system = SystemMessage(
        content=SELECT_TOOL_PROMPT.format(
            tool_names=", ".join(t.name for t in TOOLS),
            focus=state["focus"] or "No specific focus. Give a general overview of the repo.",
            name=meta["full_name"],
            description=meta.get("description") or "no description",
            language=meta.get("language") or "unknown",
            stars=meta.get("stars", 0),
            total_files=meta.get("total_files", len(state["file_tree"])),
            top_level=top_level,
            files_read="\n".join(state["files_read"]) or "(none yet)",
            findings=_format_findings(state["findings"]),
            next_hint=state["next_hint"] or "(none)",
        )
    )

    prompt = [system, HumanMessage("Choose your next tool call."), *state["messages"]]
    try:
        response = await ainvoke_tool_call(_llm_with_tools(), prompt)
    except RuntimeError:
        # The model keeps failing. Do not crash: stop here and write the report from what we have.
        return {"stop_reason": "model_error", "step": state["step"] + 1}

    usage = response.usage_metadata or {}
    return {
        "messages": [response],
        "step": state["step"] + 1,
        "tokens_in": state["tokens_in"] + usage.get("input_tokens", 0),
        "tokens_out": state["tokens_out"] + usage.get("output_tokens", 0),
    }


def route_after_choose(state: InvestigationState) -> str:
    return "write_report" if state["stop_reason"] else "tools"