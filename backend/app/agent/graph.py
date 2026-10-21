from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from app.agent.nodes.final_node import write_report
from app.agent.nodes.first_node import fetch_tree
from app.agent.nodes.second_node import route_after_choose, select_next_tool
from app.agent.nodes.third_node import evaluate_progress, route_after_evaluate
from app.agent.state import InvestigationState
from app.agent.tools import TOOLS
from app.core.config import settings


def build_graph(checkpointer=None):
    graph = StateGraph(InvestigationState)

    graph.add_node("fetch_tree", fetch_tree)
    graph.add_node("choose_tool", select_next_tool)
    graph.add_node("tools", ToolNode(TOOLS))
    graph.add_node("evaluate", evaluate_progress)
    graph.add_node("write_report", write_report)

    graph.add_edge(START, "fetch_tree")
    graph.add_edge("fetch_tree", "choose_tool")
    graph.add_conditional_edges(
        "choose_tool", route_after_choose, {"tools": "tools", "write_report": "write_report"}
    )
    graph.add_edge("tools", "evaluate")
    graph.add_conditional_edges(
        "evaluate", route_after_evaluate, {"choose_tool": "choose_tool", "write_report": "write_report"}
    )
    graph.add_edge("write_report", END)

    return graph.compile(checkpointer=checkpointer)


agent_graph = build_graph()


def make_initial_state(investigation_id: str, repo_url: str, owner: str, repo: str, focus: str | None) -> dict:
    """Every field must exist, because the nodes read them with state[...]."""
    return {
        "investigation_id": investigation_id,
        "repo_url": repo_url,
        "owner": owner,
        "repo": repo,
        "focus": (focus or "").strip() or None,   # spaces-only focus becomes None
        "metadata": {},
        "file_tree": [],
        "messages": [],
        "files_read": [],
        "chunks_read": [],
        "findings": [],
        "step": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "stop_reason": None,
        "next_hint": None,
        "report": None,
    }


def run_config() -> dict:
    # Each loop is 3 graph steps (choose_tool, tools, evaluate), and LangGraph's
    # default limit is only 25, so a normal run would crash with GraphRecursionError.
    return {"recursion_limit": settings.max_agent_steps * 3 + 10}

APPENDED_FIELDS = ("findings", "files_read", "chunks_read")   # fields that accumulate


def merge_update(state: dict, changes: dict) -> dict:
    """Apply one node's returned changes to our running copy of the state."""
    merged = {**state}
    for key, value in changes.items():
        if key == "messages":
            continue   # we don't need the message history after the run
        merged[key] = [*state[key], *value] if key in APPENDED_FIELDS else value
    return merged