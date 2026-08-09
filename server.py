"""
GitHub MCP Toolkit — FastMCP Server Entry Point
================================================
Registers all 11 MCP tools (8 core + 3 advanced) and configures
the structured JSON audit logger before starting the stdio transport.
"""

import os
import time
import json
import logging

try:
    from fastmcp import FastMCP
except ImportError:
    from mcp.server.fastmcp import FastMCP

from github_client import GitHubClient
from tools import (
    get_open_issues,
    search_issues,
    create_issue,
    add_label,
    close_issue,
    bulk_label_stale_issues,
    triage_issue,
    get_rate_limit_status,
    list_repositories,
    # Advanced tools
    semantic_search_issues,
    undo_last_action,
    get_transaction_history,
    get_trace_history,
)

# ---------------------------------------------------------------------------
# Structured audit logger — writes one JSON object per line to tool_calls.log
# ---------------------------------------------------------------------------
LOG_FILE = os.path.join(os.path.dirname(__file__), "tool_calls.log")
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("github_mcp_toolkit")


def log_audit_trail(
    tool_name: str,
    args: dict,
    status: str,
    duration_ms: float,
    error: str = "",
) -> None:
    """Record a structured JSON tool-invocation audit entry."""
    log_entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tool": tool_name,
        # Redact any key that looks like a secret token
        "args": {k: v for k, v in args.items() if "token" not in k.lower()},
        "status": status,
        "duration_ms": round(duration_ms, 2),
        "error": error,
    }
    logger.info(json.dumps(log_entry))


# ---------------------------------------------------------------------------
# Server & client initialisation
# ---------------------------------------------------------------------------
mcp = FastMCP("github-mcp-toolkit")
gh = GitHubClient()

# ---------------------------------------------------------------------------
# Tool registration — core tools
# ---------------------------------------------------------------------------
get_open_issues.register(mcp, gh)
search_issues.register(mcp, gh)
create_issue.register(mcp, gh)
add_label.register(mcp, gh)
close_issue.register(mcp, gh)
bulk_label_stale_issues.register(mcp, gh)
triage_issue.register(mcp, gh)
get_rate_limit_status.register(mcp, gh)
list_repositories.register(mcp, gh)

# ---------------------------------------------------------------------------
# Tool registration — advanced tools (Vector, Saga, Policy)
# ---------------------------------------------------------------------------
semantic_search_issues.register(mcp, gh)
undo_last_action.register(mcp, gh)
get_transaction_history.register(mcp, gh)

get_trace_history.register(mcp, gh)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    transport = os.getenv("MCP_TRANSPORT", "stdio")  # "stdio" for Claude Desktop, "sse" for Docker/network
    logger.info(f"GitHub MCP Server starting — 12 tools registered (8 core + 4 advanced). Transport: {transport}")
    mcp.run(transport=transport)
