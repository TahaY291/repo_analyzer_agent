import asyncio
import json
import sys

from app.agent.github_client import github_client, parse_repo_url
from backend.app.agent.nodes.first_node import fetch_tree


async def main(url: str) -> None:
    owner, repo = parse_repo_url(url)
    update = await fetch_tree({"owner": owner, "repo": repo, "step": 0})  # type: ignore[typeddict-item]

    print(json.dumps(update["metadata"], indent=2))
    print(f"\nstep -> {update['step']}")
    print(f"{len(update['file_tree'])} paths in file_tree. First 20:")
    for path in update["file_tree"][:20]:
        print("  ", path)
    await github_client.aclose()


asyncio.run(main(sys.argv[1]))