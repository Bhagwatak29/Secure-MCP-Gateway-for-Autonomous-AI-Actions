# GitHub Issues MCP Server

A custom MCP (Model Context Protocol) server that lets an AI model securely
read and manage GitHub issues, with:

- **Authenticated access** to a private/personal GitHub account via a token
- **Confirmation prompts** in front of every write/delete-equivalent action
- **Metrics tracking** on tool-call success/failure so you can measure how
  reliably the model picks the right tool and uses it correctly

## 1. Setup

```bash
cd mcp-github-server
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# edit .env: paste in a GitHub token (Settings -> Developer settings ->
# Personal access tokens -> Fine-grained tokens, with Issues read/write
# permission on the repo(s) you want to use), and optionally restrict
# ALLOWED_REPOS.
```

## 2. Try it locally with the MCP Inspector

```bash
mcp dev server.py
```

This opens a browser UI where you can call `list_issues`, `get_issue`,
`create_issue`, `add_comment`, and `close_issue` directly and see the
confirmation prompt fire for the write tools.

## 3. Connect it to Claude Desktop (or another MCP client)

```bash
mcp install server.py --name "GitHub Issues"
```

Or add manually to your client's MCP config:

```json
{
  "mcpServers": {
    "github-issues": {
      "command": "python",
      "args": ["/absolute/path/to/mcp-github-server/server.py"]
    }
  }
}
```

## 4. Tools exposed

| Tool | Type | Confirmation required? |
|---|---|---|
| `list_issues(owner, repo, state, limit)` | read | No |
| `get_issue(owner, repo, issue_number)` | read | No |
| `create_issue(owner, repo, title, body)` | write | **Yes** |
| `add_comment(owner, repo, issue_number, comment)` | write | **Yes** |
| `close_issue(owner, repo, issue_number)` | write | **Yes** |

Write tools use MCP **elicitation** (`ctx.elicit(...)`) to pause the tool
call and ask a human to accept/decline before anything touches GitHub. If
the human declines or cancels, nothing is written and that's logged as a
"declined" outcome in metrics.

GitHub's API doesn't support deleting issues (only GitHub Apps with special
permissions can, and it's rarely allowed) — `close_issue` is the closest
"destructive" action, and it's gated the same way creates/comments are.

## 5. Metrics: measuring tool-selection quality

Every tool call — success, failure, and confirmation outcome — is logged
to a local SQLite DB (`metrics.db` by default). View a summary with:

```bash
python metrics_dashboard.py
```

This reports, per tool:
- **Success rate** — low success rate on one tool suggests its description
  or argument schema is confusing the model relative to the others
- **Top error category** — `validation` errors mean the model picked the
  right tool but passed bad arguments (usually fixable by tightening the
  docstring); `auth`/`not_found` are config issues, not model issues
- **Confirmation decline rate** — how often a human rejected a write action
  the model proposed; a high rate suggests the model is reaching for
  create/comment/close in situations where it shouldn't

You can also query `metrics.db` directly with any SQLite tool for deeper
analysis (e.g. plotting trends over time as you iterate on tool
descriptions).

## 6. Production hardening ideas (next steps)

- Swap the raw PAT for a GitHub App installation token (shorter-lived,
  scoped per-installation, better audit trail)
- Add retry/backoff on `rate_limit` errors instead of failing immediately
- Add structured logging (e.g. to a real DB or observability platform)
  instead of SQLite once you're running this for more than one user
- Add per-tool rate limiting so a misbehaving model can't spam GitHub
- Write a small eval set of prompts with known "correct tool" labels so you
  can measure true tool-selection accuracy, not just success/failure proxies
