from typing import Annotated

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from app.agent.github_client import BinaryFile, FileNotInRepo, github_client
from app.agent.tree_view import list_directory_entries, normalize_directory
from app.core.config import settings


@tool
def list_directory(directory: str, state: Annotated[dict, InjectedState]) -> str:
    """List the files and folders directly inside one folder of the repository.

    Use an empty string for the top level, or a path such as "src/api". Folders show
    how many files they contain. Use this to explore before deciding which files to read.
    """
    listing = list_directory_entries(state["file_tree"], directory, settings.max_listing_entries)
    if listing is None:
        return "ERROR: that folder does not exist. Use a folder name exactly as shown."
    name = normalize_directory(directory) or "(top level)"
    return f"Contents of {name}:\n{listing}"


@tool
async def read_repository_file(
    file_path: str,
    state: Annotated[dict, InjectedState],
    offset: int = 0,
) -> str:
    """Read one text file from the repository being investigated.

    Give the path exactly as it appears in the file list, for example "src/main.py".
    Long files come in parts. If the result says more remains, call this tool again with
    the offset it gives to read the next part. Binary files cannot be read.
    """
    owner, repo = state["owner"], state["repo"]
    branch = state["metadata"]["default_branch"]
    offset = max(offset, 0)

    if file_path not in state["file_tree"]:
        return "ERROR: that path is not in the file list. Use a path exactly as listed."
    if f"{file_path}@{offset}" in state["chunks_read"]:
        return (
            "ERROR: this part of the file was already read and its summary is in FINDINGS. "
            "Choose another file, or a later offset."
        )

    try:
        file = await github_client.get_file(owner, repo, file_path, branch, offset=offset)
    except (FileNotInRepo, BinaryFile) as e:
        return f"ERROR: {e}"
    # Rate limits and outages are NOT caught here: they must stop the whole run.

    if file.total_chars > 0 and offset >= file.total_chars:
        return f"ERROR: offset {offset} is past the end of the file ({file.total_chars} characters)."

    end = offset + len(file.text)
    header = f"FILE: {file.path} (characters {offset}-{end} of {file.total_chars}"
    header += f"; more remains, continue with offset={end})" if file.truncated else ")"
    return f"{header}\n{file.text}"


@tool
async def search_in_file(
    file_path: str,
    query: str,
    state: Annotated[dict, InjectedState],
) -> str:
    """Find where a word or name (for example a function or class) appears inside ONE repository file.

    Returns matching lines with their line number and character offset. Then call
    read_repository_file with that offset to read the code around it.
    """
    owner, repo = state["owner"], state["repo"]
    branch = state["metadata"]["default_branch"]

    if file_path not in state["file_tree"]:
        return "ERROR: that path is not in the file list. Use a path exactly as listed."
    if not query.strip():
        return "ERROR: query is empty."

    try:
        file = await github_client.get_file(
            owner, repo, file_path, branch, limit=settings.max_search_file_chars
        )
    except (FileNotInRepo, BinaryFile) as e:
        return f"ERROR: {e}"

    needle = query.strip().lower()
    matches: list[str] = []
    pos = 0
    for number, line in enumerate(file.text.splitlines(keepends=True), start=1):
        if needle in line.lower():
            matches.append(f"line {number}, offset {pos}: {line.strip()[:160]}")
            if len(matches) >= settings.max_search_matches:
                break
        pos += len(line)

    if not matches:
        return f"No matches for '{query}' in {file_path}."
    note = f"\n(only the first {len(file.text)} characters were searched)" if file.truncated else ""
    return f"Matches for '{query}' in {file_path}:\n" + "\n".join(matches) + note


TOOLS = [list_directory, read_repository_file, search_in_file]