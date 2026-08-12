"""
MCP server exposing GitHub Issues to an AI model.

Read-only tools (list_issues, get_issue) run immediately.
Write tools (create_issue, add_comment, close_issue) are gated behind
confirmation two ways:

1. Preferred: MCP elicitation (ctx.elicit) - the server itself pauses and
   asks the connected client to confirm before writing. This works in
   clients that support elicitation (e.g. the MCP Inspector).
2. Fallback: some MCP hosts (e.g. Claude Desktop, as of testing this)
   don't yet implement elicitation and respond "Method not found." In that
   case we catch the failure and fall back to relying on the *host's own*
   tool-approval gate (the "Needs approval" setting you configure per tool
   in Claude Desktop's Extension settings) - the write still only happens
   after a human approved it, just via the host's UI instead of ours.
   This fallback is logged distinctly in metrics so you can see which path
   was actually used in practice.

Run locally with:
    mcp dev server.py          # opens the MCP Inspector
Or wire it into Claude Desktop / another MCP client via stdio:
    mcp install server.py
"""
from typing import Literal

from pydantic import BaseModel, Field
from mcp.server.fastmcp import FastMCP, Context
from mcp.server.elicitation import AcceptedElicitation, DeclinedElicitation, CancelledElicitation

import config
import github_client as gh
from github_client import GitHubClientError
from metrics import track

config.check_config()

mcp = FastMCP("github-issues")


def _repo_guard(owner: str, repo: str):
    if not config.is_repo_allowed(owner, repo):
        raise GitHubClientError(
            f"Access to {owner}/{repo} is not permitted by this server's ALLOWED_REPOS config.",
            category="auth",
        )


# ---------------------------------------------------------------------
# Read-only tools — no confirmation needed
# ---------------------------------------------------------------------

@mcp.tool()
@track("list_issues", requires_confirmation=False)
def list_issues(
    owner: str,
    repo: str,
    state: Literal["open", "closed", "all"] = "open",
    limit: int = 20,
) -> list[dict]:
    """List issues in a GitHub repository.

    Use this to see what issues currently exist before deciding whether to
    create, comment on, or close one. Does NOT modify anything.

    Args:
        owner: Repository owner/organization, e.g. "anthropics"
        repo: Repository name, e.g. "claude-code"
        state: Filter by "open", "closed", or "all". Defaults to "open".
        limit: Max number of issues to return (1-100). Defaults to 20.
    """
    _repo_guard(owner, repo)
    issues = gh.list_issues(owner, repo, state=state, limit=limit)
    return [
        {
            "number": i["number"],
            "title": i["title"],
            "state": i["state"],
            "url": i["html_url"],
            "author": i["user"]["login"],
            "created_at": i["created_at"],
            "comments": i["comments"],
        }
        for i in issues
    ]


@mcp.tool()
@track("get_issue", requires_confirmation=False)
def get_issue(owner: str, repo: str, issue_number: int) -> dict:
    """Get full details of a single GitHub issue, including its body text.

    Args:
        owner: Repository owner/organization
        repo: Repository name
        issue_number: The issue number (the integer shown after '#' on GitHub)
    """
    _repo_guard(owner, repo)
    i = gh.get_issue(owner, repo, issue_number)
    return {
        "number": i["number"],
        "title": i["title"],
        "state": i["state"],
        "body": i["body"],
        "url": i["html_url"],
        "author": i["user"]["login"],
        "created_at": i["created_at"],
        "comments": i["comments"],
    }


# ---------------------------------------------------------------------
# Write tools — gated behind an explicit human confirmation prompt
# ---------------------------------------------------------------------

class _Confirm(BaseModel):
    confirm: bool = Field(description="Type true to proceed, false to cancel")


async def _try_elicit_confirm(ctx: Context, message: str):
    """
    Try MCP elicitation for confirmation. Returns one of:
      "accepted"        - server-level elicitation ran and the human approved
      "declined"         - server-level elicitation ran and the human declined
      "cancelled"        - server-level elicitation ran and the human cancelled
      "host_gated"       - elicitation isn't supported by this client; we're
                            trusting the host's own tool-approval gate instead
                            (the write proceeds - if a human hadn't approved,
                            the host would never have called this tool at all)
    """
    try:
        result = await ctx.elicit(message=message, schema=_Confirm)
    except Exception:
        # Client doesn't support elicitation (e.g. "Method not found").
        # Fall back to trusting the host's own approval gate.
        return "host_gated"

    match result:
        case AcceptedElicitation(data=data) if data.confirm:
            return "accepted"
        case AcceptedElicitation():
            return "declined"
        case DeclinedElicitation():
            return "declined"
        case CancelledElicitation():
            return "cancelled"
    return "declined"


@mcp.tool()
@track("create_issue", requires_confirmation=True)
async def create_issue(owner: str, repo: str, title: str, body: str, ctx: Context) -> dict:
    """Create a new issue in a GitHub repository. THIS WRITES DATA.

    The human operator must confirm before anything is created - either via
    this server's own confirmation prompt, or via the host app's tool
    approval setting if the host doesn't support server-side confirmation.

    Args:
        owner: Repository owner/organization
        repo: Repository name
        title: Issue title (required, non-empty)
        body: Issue description/body text
    """
    _repo_guard(owner, repo)
    outcome = await _try_elicit_confirm(
        ctx, f"Create a new issue in {owner}/{repo}?\nTitle: {title}\nBody: {body[:200]}"
    )
    if outcome in ("accepted", "host_gated"):
        issue = gh.create_issue(owner, repo, title, body)
        return {
            "_confirmation": outcome,
            "created": True,
            "number": issue["number"],
            "url": issue["html_url"],
        }
    return {"_confirmation": outcome, "created": False, "reason": f"Not confirmed ({outcome})"}


@mcp.tool()
@track("add_comment", requires_confirmation=True)
async def add_comment(owner: str, repo: str, issue_number: int, comment: str, ctx: Context) -> dict:
    """Add a comment to an existing GitHub issue. THIS WRITES DATA.

    The human operator must confirm before posting - either via this
    server's own confirmation prompt, or via the host app's tool approval
    setting if the host doesn't support server-side confirmation.

    Args:
        owner: Repository owner/organization
        repo: Repository name
        issue_number: The issue number to comment on
        comment: The comment text to post
    """
    _repo_guard(owner, repo)
    outcome = await _try_elicit_confirm(
        ctx, f"Post this comment on {owner}/{repo}#{issue_number}?\n\n{comment[:300]}"
    )
    if outcome in ("accepted", "host_gated"):
        c = gh.add_comment(owner, repo, issue_number, comment)
        return {"_confirmation": outcome, "posted": True, "url": c.get("html_url")}
    return {"_confirmation": outcome, "posted": False, "reason": f"Not confirmed ({outcome})"}


@mcp.tool()
@track("close_issue", requires_confirmation=True)
async def close_issue(owner: str, repo: str, issue_number: int, ctx: Context) -> dict:
    """Close an open GitHub issue. THIS WRITES DATA (state change).

    The human operator must confirm before closing - either via this
    server's own confirmation prompt, or via the host app's tool approval
    setting if the host doesn't support server-side confirmation.

    Args:
        owner: Repository owner/organization
        repo: Repository name
        issue_number: The issue number to close
    """
    _repo_guard(owner, repo)
    outcome = await _try_elicit_confirm(ctx, f"Close issue {owner}/{repo}#{issue_number}?")
    if outcome in ("accepted", "host_gated"):
        i = gh.close_issue(owner, repo, issue_number)
        return {"_confirmation": outcome, "closed": True, "state": i["state"]}
    return {"_confirmation": outcome, "closed": False, "reason": f"Not confirmed ({outcome})"}


if __name__ == "__main__":
    mcp.run()
