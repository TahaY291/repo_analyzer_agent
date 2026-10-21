import asyncio
import re
from dataclasses import dataclass
from urllib.parse import quote, urlparse

import httpx

from app.core.config import settings

OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")

IGNORED_DIRS = {
    ".git", "node_modules", "dist", "build", "out", ".next", "vendor", "target",
    "__pycache__", ".venv", "venv", "coverage", ".idea", ".pytest_cache",
    ".mypy_cache", "bower_components", ".gradle", "Pods",
}
IGNORED_FILES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Pipfile.lock",
    "Cargo.lock", "go.sum", "composer.lock", "Gemfile.lock", "uv.lock", "bun.lockb",
    ".DS_Store", "Thumbs.db",
}
IGNORED_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg", ".bmp", ".pdf",
    ".zip", ".tar", ".gz", ".tgz", ".7z", ".rar",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".mp3", ".mp4", ".mov", ".avi", ".webm",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".class", ".jar", ".pyc", ".o", ".a",
    ".map", ".min.js", ".min.css", ".lock",
    ".sqlite", ".db", ".parquet", ".onnx", ".pt", ".h5", ".pkl",
)
MAX_FILE_BYTES_IN_TREE = 1_000_000   # bigger files are almost never source code


# ---------- errors (the agent's own; the API layer translates them later) ----------

class GitHubError(Exception):
    """Anything that went wrong while talking to GitHub."""


class InvalidRepoURL(GitHubError):
    pass


class RepoNotFound(GitHubError):
    pass


class RateLimited(GitHubError):
    def __init__(self, reset_at: int | None = None) -> None:
        self.reset_at = reset_at   # unix time when the limit resets, if GitHub told us
        super().__init__("GitHub rate limit reached")


class GitHubUnavailable(GitHubError):
    pass

class FileNotInRepo(GitHubError):
    pass

class BinaryFile(GitHubError):
    pass

# ---------- pure helpers (no network, easy to unit test) ----------

def parse_repo_url(url: str) -> tuple[str, str]:
    """Strictly turn a GitHub URL into (owner, repo). Never fetches anything."""
    try:
        parsed = urlparse(url.strip())
        host, port = parsed.hostname, parsed.port
    except ValueError:
        raise InvalidRepoURL("That does not look like a valid URL")

    if parsed.scheme not in ("http", "https") or host not in ("github.com", "www.github.com"):
        raise InvalidRepoURL("Only github.com repository URLs are supported")
    if parsed.username or parsed.password or port:
        raise InvalidRepoURL("The URL must not contain credentials or a port")

    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        raise InvalidRepoURL("The URL must look like https://github.com/owner/repo")

    owner, repo = parts[0], parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not OWNER_RE.match(owner) or not REPO_RE.match(repo) or repo in (".", ".."):
        raise InvalidRepoURL("Invalid owner or repository name")
    return owner, repo


@dataclass
class RepoTree:
    paths: list[str]            # what the agent will see
    total_files: int            # files left after filtering, before the cap
    omitted: int                # files that did not fit under the cap
    truncated_by_github: bool   # GitHub itself cut the listing (huge repos)

@dataclass
class FileContent:
    path: str
    text: str
    total_chars: int
    truncated: bool

def _is_useful(path: str, size: int) -> bool:
    name = path.rsplit("/", 1)[-1]
    if name in IGNORED_FILES or path.lower().endswith(IGNORED_SUFFIXES):
        return False
    if size > MAX_FILE_BYTES_IN_TREE:
        return False
    return not any(part in IGNORED_DIRS for part in path.split("/")[:-1])


def select_paths(blobs: list[dict], cap: int, truncated: bool = False) -> RepoTree:
    """Drop junk, then keep the `cap` shallowest paths (important files tend to be near the root)."""
    useful = [b["path"] for b in blobs if _is_useful(b["path"], b.get("size") or 0)]
    useful.sort(key=lambda p: (p.count("/"), p.lower()))
    return RepoTree(
        paths=useful[:cap],
        total_files=len(useful),
        omitted=max(0, len(useful) - cap),
        truncated_by_github=truncated,
    )


# ---------- the client ----------

class GitHubClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "repo-intelligence-agent",   # GitHub rejects requests without one
        }
        if settings.github_token:
            headers["Authorization"] = f"Bearer {settings.github_token}"
        self._http = httpx.AsyncClient(
            base_url=settings.github_api_base,
            headers=headers,
            timeout=settings.github_timeout_seconds,
            follow_redirects=True,   # renamed repos answer with a redirect
            transport=transport,     # tests pass a fake transport here
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, headers: dict[str , str] | None = None,  **params: str) -> httpx.Response:
        """GET with up to 3 attempts for network errors and 5xx. Client errors are not retried."""
        for attempt in range(3):
            try:
                response = await self._http.get(path, params=params or None , headers=headers)
                if response.status_code < 500:
                    return self._raise_for_status(response)
            except httpx.TransportError:
                pass   # timeout or connection problem: try again
            if attempt < 2:
                await asyncio.sleep(0.5 * 2**attempt)
        raise GitHubUnavailable("GitHub is not responding. Please try again later.")

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> httpx.Response:
        code = response.status_code
        if code == 404:
            raise RepoNotFound("Repository not found (it may be private)")
        if code in (403, 429):
            limited = (
                code == 429
                or response.headers.get("x-ratelimit-remaining") == "0"
                or "retry-after" in response.headers
            )
            if limited:
                reset = response.headers.get("x-ratelimit-reset", "")
                raise RateLimited(int(reset) if reset.isdigit() else None)
            raise GitHubError("GitHub denied access to this repository")
        if code == 409:
            raise GitHubError("This repository is empty")
        if code >= 400:
            raise GitHubError(f"GitHub returned an unexpected status ({code})")
        return response

    async def get_metadata(self, owner: str, repo: str) -> dict:
        data = (await self._get(f"/repos/{owner}/{repo}")).json()
        if data.get("private"):
            raise RepoNotFound("Private repositories are not supported")
        return {
            "full_name": data["full_name"],
            "description": data.get("description"),
            "language": data.get("language"),
            "stars": data.get("stargazers_count", 0),
            "forks": data.get("forks_count", 0),
            "open_issues": data.get("open_issues_count", 0),
            "topics": data.get("topics", []),
            "license": (data.get("license") or {}).get("spdx_id"),
            "default_branch": data["default_branch"],
            "size_kb": data.get("size", 0),
            "archived": data.get("archived", False),
            "is_fork": data.get("fork", False),
            "pushed_at": data.get("pushed_at"),
            "html_url": data.get("html_url"),
        }

    async def get_tree(self, owner: str, repo: str, ref: str) -> RepoTree:
        path = f"/repos/{owner}/{repo}/git/trees/{quote(ref, safe='')}"
        data = (await self._get(path, recursive="1")).json()
        blobs = [item for item in data.get("tree", []) if item.get("type") == "blob"]
        return select_paths(blobs, settings.max_tree_paths, truncated=bool(data.get("truncated")))

    async def get_file(
        self, owner: str, repo: str, path: str, ref: str, offset: int = 0, limit: int | None = None
    ) -> FileContent:
        url = f"/repos/{owner}/{repo}/contents/{quote(path, safe='/')}"
        try:
            response = await self._get(
                url, headers={"Accept": "application/vnd.github.raw+json"}, ref=ref
            )
        except RepoNotFound:
            raise FileNotInRepo(f"File not found: {path}")

        raw = response.content
        if b"\x00" in raw[:8000]:
            raise BinaryFile(f"{path} is a binary file")

        text = raw.decode("utf-8", errors="replace")
        size = limit or settings.max_file_chars
        chunk = text[offset : offset + size]
        return FileContent(
            path=path,
            text=chunk,
            total_chars=len(text),
            truncated=offset + size < len(text),   # True means "more remains after this chunk"
        )

# One shared client for the whole app (connection reuse). Closed at shutdown.
github_client = GitHubClient()