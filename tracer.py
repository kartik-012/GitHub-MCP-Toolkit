"""
Execution Tracer — GitHub MCP Toolkit
======================================
A zero-dependency, OpenTelemetry-inspired span-based tracer.

Each tool call opens a root Trace. Internal phases (policy check, vector scan,
API call, saga commit) are recorded as child Spans with timing and status.

Traces are appended to `traces.jsonl` — one JSON object per line — for
structured inspection, debugging, and the `get_trace_history` tool.

Design: intentionally no external deps. Replaces the need for a full
OpenTelemetry SDK while demonstrating the same distributed tracing concepts.
"""

import os
import json
import time
import uuid
import logging
from contextlib import contextmanager
from typing import List, Dict, Any, Optional

logger = logging.getLogger("github_mcp_toolkit")

TRACES_FILE = os.path.join(os.path.dirname(__file__), "traces.jsonl")


class Span:
    """A single named phase within a tool execution trace."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._start: float = time.perf_counter()
        self.duration_ms: Optional[float] = None
        self.status: str = "running"
        self.metadata: Dict[str, Any] = {}

    def finish(self, status: str = "ok", **metadata: Any) -> None:
        self.duration_ms = round((time.perf_counter() - self._start) * 1000, 2)
        self.status = status
        self.metadata.update(metadata)

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "span": self.name,
            "duration_ms": self.duration_ms,
            "status": self.status,
        }
        if self.metadata:
            d["meta"] = self.metadata
        return d


class Trace:
    """
    Root trace for a single tool invocation.
    Collect spans, then call `.commit()` to flush to traces.jsonl.
    """

    def __init__(self, tool_name: str) -> None:
        self.trace_id: str = f"tr_{uuid.uuid4().hex[:8]}"
        self.tool_name: str = tool_name
        self.timestamp: str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._wall_start: float = time.perf_counter()
        self._spans: List[Span] = []
        self._active_span: Optional[Span] = None

    # ------------------------------------------------------------------
    # Span management
    # ------------------------------------------------------------------

    @contextmanager
    def span(self, name: str):
        """Context manager that records a child span."""
        s = Span(name)
        self._spans.append(s)
        self._active_span = s
        try:
            yield s
            if s.status == "running":
                s.finish(status="ok")
        except Exception as exc:
            s.finish(status="error", error=str(exc))
            raise
        finally:
            self._active_span = None

    # ------------------------------------------------------------------
    # Commit to disk
    # ------------------------------------------------------------------

    def commit(self, outcome: str = "success") -> None:
        """Append the completed trace to traces.jsonl."""
        total_ms = round((time.perf_counter() - self._wall_start) * 1000, 2)
        record = {
            "trace_id": self.trace_id,
            "tool": self.tool_name,
            "timestamp": self.timestamp,
            "outcome": outcome,
            "total_ms": total_ms,
            "spans": [s.to_dict() for s in self._spans],
        }
        try:
            with open(TRACES_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as e:
            logger.warning(f"[Tracer] Failed to write trace: {e}")


# ---------------------------------------------------------------------------
# Reader — used by get_trace_history tool
# ---------------------------------------------------------------------------

def read_recent_traces(limit: int = 10) -> List[Dict[str, Any]]:
    """Return the most recent `limit` traces from traces.jsonl (newest first)."""
    if not os.path.exists(TRACES_FILE):
        return []
    try:
        with open(TRACES_FILE, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]
        records = [json.loads(line) for line in lines]
        return list(reversed(records))[:limit]
    except Exception as e:
        logger.warning(f"[Tracer] Failed to read traces: {e}")
        return []
