import asyncio
import sys

from app.agent.checkpoint import open_checkpointer
from app.agent.github_client import github_client
from app.agent.graph import build_graph, make_initial_state, run_config

THREAD = "resume-test-1"   # change this to start a fresh run
URL, OWNER, REPO = "https://github.com/pallets/click", "pallets", "click"


async def main(mode: str):
    async with open_checkpointer() as saver:
        graph = build_graph(saver)
        config = {**run_config(), "configurable": {"thread_id": THREAD}}
        graph_input = make_initial_state(THREAD, URL, OWNER, REPO, "How does option parsing work?") if mode == "start" else None
        async for update in graph.astream(graph_input, config=config, stream_mode="updates"):
            for node in update:
                print("done:", node)
    await github_client.aclose()


asyncio.run(main(sys.argv[1]), loop_factory=asyncio.SelectorEventLoop)