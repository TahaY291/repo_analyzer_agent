import asyncio
import json
import sys

from app.agent.github_client import github_client
from app.agent.graph import agent_graph, make_initial_state, run_config
import re

_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

REPO_URL = "https://github.com/pallets/click"     # small, well known, change if you like
OWNER, REPO = "pallets", "click"

def code_terms(text: str) -> set[str]:
    """Words that look like code names: snake_case or camelCase/PascalCase."""
    terms = set()
    for tok in _TOKEN.findall(text):
        t = tok.strip("_")
        if len(t) > 3 and ("_" in t or re.search(r"[a-z][A-Z]", t)):
            terms.add(t.lower())
    return terms


def audit_report(report: dict) -> list[str]:
    """Code names the report uses that no finding mentions."""
    evidence = " ".join(
        [f["file"] + " " + f["summary"] for f in report["findings"]]
        + [report["repo"].get("description") or ""]
    )
    claims = " ".join(
        [report["title"], report["summary"], *report["key_takeaways"], *report["limitations"]]
        + [s["heading"] + " " + s["body"] for s in report["sections"]]
    )
    return sorted(code_terms(claims) - code_terms(evidence))



async def stage1():
    """GitHub client only. No LLM."""
    meta = await github_client.get_metadata(OWNER, REPO)
    tree = await github_client.get_tree(OWNER, REPO, meta["default_branch"])
    print(meta["full_name"], meta["language"], "files:", tree.total_files)
    print(tree.paths[:10])
    f = await github_client.get_file(OWNER, REPO, "README.md", meta["default_branch"])
    print("README chars:", f.total_chars, "truncated:", f.truncated)


async def full(focus: str | None):
    """Whole graph. Prints every node as it finishes."""
    state = make_initial_state("test-1", REPO_URL, OWNER, REPO, focus)
    final = state
    async for update in agent_graph.astream(state, config=run_config(), stream_mode="updates"):
        for node, changes in update.items():
            print(f"\n--- {node} ---  keys: {list(changes)}")
            if node == "tools":
               print("tool result:", str(changes["messages"][-1].content)[:200])
            if node == "choose_tool":
                print("tool calls:", changes["messages"][0].tool_calls)
            if node == "evaluate":
                print("new findings:", changes["findings"])
                print("stop_reason:", changes["stop_reason"], "| next_hint:", changes["next_hint"])
            final = {**final, **{k: v for k, v in changes.items() if k not in ("messages", "findings", "files_read")}}
            final["findings"] = final["findings"] + changes.get("findings", [])
            final["files_read"] = final["files_read"] + changes.get("files_read", [])
    print("\n=== REPORT ===")

    print(json.dumps(final["report"], indent=2))
    print("tokens in/out:", final["tokens_in"], final["tokens_out"], "steps:", final["step"])
    if final["report"]:
        unsupported = audit_report(final["report"])
        print("\nUNSUPPORTED TERMS:", len(unsupported), unsupported)
        print("findings:", len(final["report"]["findings"]),
              "| report words:", len(json.dumps({k: v for k, v in final["report"].items() if k not in ("findings", "coverage", "repo")}).split()))


async def main():
    stage = sys.argv[1]
    try:
        if stage == "1":
            await stage1()
        elif stage == "2":
            await full("How does the command and option parsing work?")
        elif stage == "3":
            await full(None)
    finally:
        await github_client.aclose()


asyncio.run(main())