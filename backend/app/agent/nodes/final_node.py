from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.agent.llm import ainvoke_json
from app.agent.prompts import REPORT_PROMPT, REPORT_TASK_FOCUSED, REPORT_TASK_OVERVIEW
from app.agent.state import Finding, InvestigationState
from app.agent.tree_view import list_directory_entries
from app.core.config import settings

_RELEVANCE_RANK = {"high": 0, "medium": 1, "low": 2}


class ReportSection(BaseModel):
    heading: str
    body: str = Field(
        description="3-8 sentences, specific and grounded in the findings. Name real files, functions and classes."
    )
    files: list[str] = Field(default_factory=list, description="Paths from the findings that support this section")


class ReportOut(BaseModel):
    title: str
    summary: str = Field(
        description=(
            "4-6 sentences. Focused run: the direct answer to the focus. "
            "Overview run: what the repo is, what it does and how it is built."
        )
    )
    key_takeaways: list[str] = Field(
        description="4-8 short bullets: the most important things learned from the files that were read"
    )
    tech_stack: list[str] = Field(
        default_factory=list,
        description="Languages, frameworks, databases and tools that the findings actually show. Nothing assumed.",
    )
    sections: list[ReportSection] = Field(description="3-7 sections")
    focus_result: Literal["fully_answered", "partially_answered", "not_found"] | None = Field(
        default=None,
        description="Focused run only: do the findings answer the focus? Leave null for an overview run.",
    )
    limitations: list[str] = Field(
        default_factory=list, description="What the findings do not cover, or other reasons to be careful"
    )


def _sorted_findings(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: _RELEVANCE_RANK[f["relevance"]])


def _format_findings(findings: list[Finding]) -> str:
    if not findings:
        return "(none)"
    return "\n".join(f"- [{f['relevance']}] {f['file']}: {f['summary']}" for f in _sorted_findings(findings))


def _coverage_note(state: InvestigationState) -> str:
    if not state["findings"]:
        return (
            "NO FILES WERE READ. Base the report only on the repository info and the top-level listing. "
            "Say clearly in limitations that file contents were not examined."
        )
    if state["stop_reason"] in ("step_limit", "token_budget", "model_error"):
        return (
            f"The investigation was cut short ({state['stop_reason']}), so coverage may be partial. "
            "Mention this in limitations."
        )
    return ""


def _fallback_content(state: InvestigationState) -> dict:
    """Used when the LLM output cannot be parsed: build a plain report straight from the findings."""
    ordered = _sorted_findings(state["findings"])
    return {
        "title": f"Investigation of {state['metadata']['full_name']}",
        "summary": "The written summary could not be generated. The findings from each file are listed below.",
        "key_takeaways": [f["summary"] for f in ordered[:5]],
        "tech_stack": [],
        "sections": [{"heading": f["file"], "body": f["summary"], "files": [f["file"]]} for f in ordered],
        "focus_result": None,
        "limitations": ["Report generation failed, so these are the raw findings."],
    }


async def write_report(state: InvestigationState) -> dict:
    """Node 4. LLM: turn the findings into the final report. Focus is optional."""
    meta = state["metadata"]
    focus = (state["focus"] or "").strip()
    mode = "focused" if focus else "overview"

    system = SystemMessage(
        content=REPORT_PROMPT.format(
            task=REPORT_TASK_FOCUSED.format(focus=focus) if focus else REPORT_TASK_OVERVIEW,
            name=meta["full_name"],
            description=meta.get("description") or "no description",
            language=meta.get("language") or "unknown",
            stars=meta.get("stars", 0),
            topics=", ".join(meta.get("topics") or []) or "none",
            total_files=meta.get("total_files", len(state["file_tree"])),
            top_level=list_directory_entries(state["file_tree"], "", settings.max_listing_entries) or "(empty)",
            findings=_format_findings(state["findings"]),
            coverage_note=_coverage_note(state),
        )
    )

    result = await ainvoke_json(
        [system, HumanMessage("Write the final report.")],
        ReportOut,
        max_output_tokens=settings.report_max_output_tokens,
    )

    raw = result["raw"]
    usage = (raw.usage_metadata if raw else None) or {}
    parsed: ReportOut | None = result["parsed"]

    if parsed is not None:
        read = set(state["files_read"])
        content = parsed.model_dump()
        for section in content["sections"]:   # drop paths the LLM made up
            section["files"] = [p for p in section["files"] if p in read]
        if mode == "overview":
            content["focus_result"] = None
    else:
        content = _fallback_content(state)

    report = {
        "mode": mode,
        "focus": focus or None,
        **content,
        "findings": _sorted_findings(state["findings"]),   # from code: exactly what each file gave us
        "repo": {
            "full_name": meta["full_name"],
            "url": meta.get("html_url"),
            "description": meta.get("description"),
            "language": meta.get("language"),
            "stars": meta.get("stars", 0),
        },
        "coverage": {   # computed in code, not by the LLM, so it is always true
            "files_read": state["files_read"],
            "files_in_repo": meta.get("total_files"),
            "steps": state["step"],
            "stop_reason": state["stop_reason"],
        },
    }

    return {
        "report": report,
        "step": state["step"] + 1,
        "tokens_in": state["tokens_in"] + usage.get("input_tokens", 0),
        "tokens_out": state["tokens_out"] + usage.get("output_tokens", 0),
    }