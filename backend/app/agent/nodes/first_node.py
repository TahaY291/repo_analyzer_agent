from app.agent.github_client import github_client
from app.agent.state import  InvestigationState


async def fetch_tree(state: InvestigationState) -> dict:
    """Node 1. Plain code , No LLM: always the first thing we do"""
    owner , repo = state["owner"] , state["repo"]

    metadata = await github_client.get_metadata(owner , repo)
    tree = await github_client.get_tree(owner , repo , metadata["default_branch"])

    return {
        "metadata" : {
            **metadata,
            "total_files": tree.total_files,
            "files_omitted": tree.omitted,
            "tree_truncated_by_github": tree.truncated_by_github,
        },
        "file_tree": tree.paths,
        "step": state["step"] + 1,
    }
