from typing import Dict, Any
from tracer import read_recent_traces


def register(mcp, gh):
    @mcp.tool()
    def get_trace_history(limit: int = 10) -> Dict[str, Any]:
        """
        Return the most recent tool execution traces showing per-phase timing
        and status (policy_check, vector_dedup, github_api, saga_journal spans).

        Use this to inspect what happened inside a previous tool call — how long
        each phase took, whether the circuit breaker was involved, and the
        overall outcome. Useful for diagnosing slow or failed tool calls.
        """
        if limit < 1 or limit > 100:
            return {
                "status": "error",
                "type": "invalid_argument",
                "message": "limit must be between 1 and 100.",
            }
        try:
            traces = read_recent_traces(limit=limit)
            return {
                "status": "success",
                "total_records": len(traces),
                "traces": traces,
            }
        except Exception as e:
            return {
                "status": "error",
                "type": "trace_read_failed",
                "message": str(e),
            }
