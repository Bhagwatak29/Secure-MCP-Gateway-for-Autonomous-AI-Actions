"""
Metrics tracking for the MCP server's production challenge:

  "Track metrics on how often the AI selects the correct tool versus
   failing due to poor descriptions or error handling."

We can't directly observe the model's *intent*, but we can observe strong
proxies for tool-selection quality:

  1. call_outcome     - did the tool call succeed, fail validation, fail
                         auth, fail not_found, etc.
  2. confirmation      - for write tools: did the model correctly pause and
                         ask for confirmation, and did the human accept or
                         decline? A decline often means the AI picked the
                         wrong action.
  3. error_category    - buckets failures so you can tell "the model called
                         the tool with bad arguments" (validation) apart
                         from "the tool itself is broken" (unknown/network).

Everything is logged to a local SQLite DB (no server needed) so you can
query it directly or run metrics_dashboard.py for a summary.
"""
import functools
import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from config import METRICS_DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    args_redacted TEXT,
    success INTEGER NOT NULL,
    error_category TEXT,
    error_message TEXT,
    latency_ms REAL,
    required_confirmation INTEGER NOT NULL DEFAULT 0,
    confirmation_result TEXT
);
"""


@contextmanager
def _connect():
    conn = sqlite3.connect(METRICS_DB_PATH)
    try:
        conn.execute(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _redact(kwargs: dict) -> str:
    """Drop obviously sensitive fields before persisting args for debugging."""
    redacted = {}
    for k, v in kwargs.items():
        if k.lower() in ("token", "password", "secret", "ctx"):
            continue
        redacted[k] = v
    try:
        return json.dumps(redacted)[:500]
    except TypeError:
        return "<unserializable>"


def log_call(
    tool_name: str,
    success: bool,
    kwargs: dict,
    latency_ms: float,
    error_category: str | None = None,
    error_message: str | None = None,
    required_confirmation: bool = False,
    confirmation_result: str | None = None,
):
    with _connect() as conn:
        conn.execute(
            """INSERT INTO tool_calls
               (ts, tool_name, args_redacted, success, error_category,
                error_message, latency_ms, required_confirmation, confirmation_result)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.now(timezone.utc).isoformat(),
                tool_name,
                _redact(kwargs),
                1 if success else 0,
                error_category,
                (error_message or "")[:500] if error_message else None,
                latency_ms,
                1 if required_confirmation else 0,
                confirmation_result,
            ),
        )


def track(tool_name: str, requires_confirmation: bool = False):
    """
    Decorator for MCP tool functions (sync or async).

    Wraps the call, times it, and logs success/failure + error category to
    the metrics DB. Re-raises the original exception after logging so the
    MCP framework still returns a proper error to the model.

    If the wrapped function returns a dict containing a "_confirmation"
    key (set to "accepted" or "declined"), that value is logged too.
    """

    def decorator(fn):
        @functools.wraps(fn)
        async def async_wrapper(*args, **kwargs):
            start = time.perf_counter()
            try:
                result = await fn(*args, **kwargs)
                latency_ms = (time.perf_counter() - start) * 1000
                confirmation_result = None
                if isinstance(result, dict):
                    confirmation_result = result.get("_confirmation")
                log_call(
                    tool_name,
                    success=True,
                    kwargs=kwargs,
                    latency_ms=latency_ms,
                    required_confirmation=requires_confirmation,
                    confirmation_result=confirmation_result,
                )
                return result
            except Exception as e:
                latency_ms = (time.perf_counter() - start) * 1000
                category = getattr(e, "category", "unknown")
                log_call(
                    tool_name,
                    success=False,
                    kwargs=kwargs,
                    latency_ms=latency_ms,
                    error_category=category,
                    error_message=str(e),
                    required_confirmation=requires_confirmation,
                )
                raise

        @functools.wraps(fn)
        def sync_wrapper(*args, **kwargs):
            start = time.perf_counter()
            try:
                result = fn(*args, **kwargs)
                latency_ms = (time.perf_counter() - start) * 1000
                confirmation_result = None
                if isinstance(result, dict):
                    confirmation_result = result.get("_confirmation")
                log_call(
                    tool_name,
                    success=True,
                    kwargs=kwargs,
                    latency_ms=latency_ms,
                    required_confirmation=requires_confirmation,
                    confirmation_result=confirmation_result,
                )
                return result
            except Exception as e:
                latency_ms = (time.perf_counter() - start) * 1000
                category = getattr(e, "category", "unknown")
                log_call(
                    tool_name,
                    success=False,
                    kwargs=kwargs,
                    latency_ms=latency_ms,
                    error_category=category,
                    error_message=str(e),
                    required_confirmation=requires_confirmation,
                )
                raise

        import inspect
        return async_wrapper if inspect.iscoroutinefunction(fn) else sync_wrapper

    return decorator
