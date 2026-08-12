"""
Thin wrapper around the GitHub REST API (Issues endpoints only).

Design goals:
- Every call raises a `GitHubClientError` with a `.category` attribute so
  metrics.py can bucket failures (auth, not_found, rate_limit, validation,
  network, unknown) instead of lumping everything into "error".
- No retries / rate-limit backoff here on purpose — keep it simple and let
  the caller (server.py) decide policy. Add backoff later if you see
  rate_limit errors show up a lot in the metrics dashboard.
"""
import requests

from config import GITHUB_API_BASE, GITHUB_TOKEN

HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


class GitHubClientError(Exception):
    def __init__(self, message: str, category: str, status_code: int | None = None):
        super().__init__(message)
        self.category = category  # auth | not_found | rate_limit | validation | network | unknown
        self.status_code = status_code


def _headers():
    if not GITHUB_TOKEN:
        raise GitHubClientError("No GitHub token configured", category="auth")
    h = dict(HEADERS)
    h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h


def _request(method: str, path: str, **kwargs) -> dict | list:
    url = f"{GITHUB_API_BASE}{path}"
    try:
        resp = requests.request(method, url, headers=_headers(), timeout=15, **kwargs)
    except requests.exceptions.RequestException as e:
        raise GitHubClientError(f"Network error calling GitHub: {e}", category="network") from e

    if resp.status_code == 401:
        raise GitHubClientError("GitHub token is invalid or expired.", category="auth", status_code=401)
    if resp.status_code == 403:
        remaining = resp.headers.get("X-RateLimit-Remaining")
        if remaining == "0":
            raise GitHubClientError("GitHub API rate limit exceeded.", category="rate_limit", status_code=403)
        raise GitHubClientError(
            "GitHub denied access (403). Check token scopes/permissions.",
            category="auth",
            status_code=403,
        )
    if resp.status_code == 404:
        raise GitHubClientError("Repository or issue not found.", category="not_found", status_code=404)
    if resp.status_code == 422:
        raise GitHubClientError(
            f"GitHub rejected the request as invalid: {resp.text[:300]}",
            category="validation",
            status_code=422,
        )
    if not resp.ok:
        raise GitHubClientError(
            f"GitHub API error {resp.status_code}: {resp.text[:300]}",
            category="unknown",
            status_code=resp.status_code,
        )

    if resp.status_code == 204 or not resp.content:
        return {}
    return resp.json()


# ---- Read-only operations ----------------------------------------------

def list_issues(owner: str, repo: str, state: str = "open", limit: int = 20) -> list[dict]:
    if state not in ("open", "closed", "all"):
        raise GitHubClientError("state must be one of: open, closed, all", category="validation")
    limit = max(1, min(limit, 100))
    data = _request(
        "GET",
        f"/repos/{owner}/{repo}/issues",
        params={"state": state, "per_page": limit},
    )
    # GitHub's issues endpoint also returns pull requests; filter those out.
    return [issue for issue in data if "pull_request" not in issue]


def get_issue(owner: str, repo: str, issue_number: int) -> dict:
    return _request("GET", f"/repos/{owner}/{repo}/issues/{issue_number}")


# ---- Write operations (server.py gates these behind confirmation) ------

def create_issue(owner: str, repo: str, title: str, body: str = "") -> dict:
    if not title or not title.strip():
        raise GitHubClientError("Issue title cannot be empty.", category="validation")
    return _request(
        "POST",
        f"/repos/{owner}/{repo}/issues",
        json={"title": title, "body": body},
    )


def add_comment(owner: str, repo: str, issue_number: int, comment: str) -> dict:
    if not comment or not comment.strip():
        raise GitHubClientError("Comment body cannot be empty.", category="validation")
    return _request(
        "POST",
        f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
        json={"body": comment},
    )


def close_issue(owner: str, repo: str, issue_number: int) -> dict:
    return _request(
        "PATCH",
        f"/repos/{owner}/{repo}/issues/{issue_number}",
        json={"state": "closed"},
    )
