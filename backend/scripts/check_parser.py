import asyncio
from app.agent.github_client import github_client

async def check():
    meta = await github_client.get_metadata("pallets", "click")
    f = await github_client.get_file("pallets", "click", "src/click/parser.py", meta["default_branch"], limit=400_000)
    for name in ["class _Option", "def _unpack_args", "def parse_args", "def _process_args_for_options", "_process_args_for_args"]:
        print(name, "->", f.text.find(name))
    await github_client.aclose()

asyncio.run(check())