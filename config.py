"""
Configuration for the MCP GitHub server.

All secrets come from environment variables (loaded from a local .env file
in development). NEVER hardcode tokens in source.
"""
import os
from dotenv import load_dotenv

load_dotenv()

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_API_BASE = os.environ.get("GITHUB_API_BASE", "https://api.github.com")

# Where the SQLite metrics DB lives. Override in .env if you want it elsewhere
# (e.g. a mounted volume in production).
METRICS_DB_PATH = os.environ.get("METRICS_DB_PATH", "metrics.db")

# Comma-separated list of "owner/repo" the server is allowed to touch.
# Leave empty to allow any repo the token can access (fine for local dev,
# NOT recommended in production).
_allowed_raw = os.environ.get("ALLOWED_REPOS", "")
ALLOWED_REPOS = {r.strip() for r in _allowed_raw.split(",") if r.strip()}


def check_config():
    """Fail fast and loudly if required config is missing."""
    if not GITHUB_TOKEN:
        raise RuntimeError(
            "GITHUB_TOKEN is not set. Create a .env file (see .env.example) "
            "with a GitHub Personal Access Token that has at least "
            "'repo' or 'public_repo' scope, or fine-grained Issues "
            "read/write permission."
        )


def is_repo_allowed(owner: str, repo: str) -> bool:
    if not ALLOWED_REPOS:
        return True
    return f"{owner}/{repo}" in ALLOWED_REPOS
