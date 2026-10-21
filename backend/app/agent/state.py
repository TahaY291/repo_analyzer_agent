import operator
from typing import Annotated , Literal , TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

class Finding(TypedDict):
    file: str
    summary: str
    relevance: Literal["high", "medium", "low"]

class InvestigationState(TypedDict):
    investigation_id: str
    repo_url : str
    owner: str
    repo: str
    focus: str | None

    # what we know about the repo 

    metadata: dict
    file_tree: list[str]

    # memory
    messages: Annotated[list[AnyMessage] , add_messages]
    files_read: Annotated[list[str], operator.add]
    findings: Annotated[list[Finding], operator.add]

    step: int
    tokens_in: int
    tokens_out: int
    stop_reason: str | None
    next_hint: str | None

    # output
    report : dict | None

    chunks_read: Annotated[list[str], operator.add]   # e.g. "src/click/core.py@0"