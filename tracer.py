"""
Execution Tracer — GitHub MCP Toolkit
======================================
A zero-dependency, OpenTelemetry-inspired span-based tracer with context
propagation (`contextvars`) so server-level middleware and inner tool phases
share a single unified trace per tool invocation.

Traces are appended to `traces.jsonl` — one JSON object per line — for
structured inspection, debugging, and the `get_trace_history` tool.
"""

import os
import json
import time
import uuid
import logging
import threading
from contextvars import ContextVar
from contextlib import contextmanager
from typing import List, Dict, Any, Optional

logger = logging.getLogger("github_mcp_toolkit")

TRACES_FILE = os.path.join(os.path.dirname(__file__), "traces.jsonl")
MAX_TRACE_RECORDS = 1000
_TRACE_FILE_LOCK = threading.RLock()
_ACTIVE_TRACE: ContextVar[Optional["Trace"]] = ContextVar("_ACTIVE_TRACE", default=None)


class Span:
    """A single named phase within a tool execution trace (with 64-bit W3C span_id)."""

    def __init__(self, name: str, parent_span_id: str = "") -> None:
        self.name = name
        self.span_id: str = uuid.uuid4().hex[:16]
        self.parent_span_id: str = parent_span_id
        self._start: float = time.perf_counter()
        self.start_unix_ns: int = time.time_ns()
        self.end_unix_ns: Optional[int] = None
        self.duration_ms: Optional[float] = None
        self.status: str = "running"
        self.metadata: Dict[str, Any] = {}

    def finish(self, status: str = "ok", **metadata: Any) -> None:
        """Mark span as finished, calculating duration and recording final metadata."""
        self.duration_ms = round((time.perf_counter() - self._start) * 1000, 2)
        self.end_unix_ns = time.time_ns()
        self.status = status
        self.metadata.update(metadata)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize span into dictionary representation for JSON logging."""
        d: Dict[str, Any] = {
            "span": self.name,
            "span_id": self.span_id,
            "duration_ms": self.duration_ms,
            "status": self.status,
        }
        if self.parent_span_id:
            d["parent_span_id"] = self.parent_span_id
        if self.metadata:
            d["meta"] = self.metadata
        return d


class Trace:
    """
    Root trace for a single tool invocation with W3C Trace Context (`traceparent`)
    and OpenTelemetry OTLP export support.
    Collects spans and flushes to `traces.jsonl` on `.commit()`.
    """

    def __init__(self, tool_name: str, parent_traceparent: Optional[str] = None) -> None:
        parsed_parent = parse_w3c_traceparent(parent_traceparent) if parent_traceparent else None
        self.w3c_trace_id: str = parsed_parent["trace_id"] if parsed_parent else uuid.uuid4().hex
        self.root_span_id: str = uuid.uuid4().hex[:16]
        self.parent_span_id: str = parsed_parent["parent_id"] if parsed_parent else ""
        self.trace_id: str = f"tr_{self.w3c_trace_id[:8]}"
        self.traceparent: str = f"00-{self.w3c_trace_id}-{self.root_span_id}-01"
        self.tool_name: str = tool_name
        self.timestamp: str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.start_unix_ns: int = time.time_ns()
        self._wall_start: float = time.perf_counter()
        self._spans: List[Span] = []
        self._active_span: Optional[Span] = None
        self._committed: bool = False

    @contextmanager
    def span(self, name: str):
        """Context manager that records a child span linked to root_span_id."""
        s = Span(name, parent_span_id=self.root_span_id)
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

    def commit(self, outcome: str = "success") -> Dict[str, Any]:
        """Append the completed trace to traces.jsonl with bounded ring-buffer retention."""
        total_ms = round((time.perf_counter() - self._wall_start) * 1000, 2)
        record = {
            "trace_id": self.trace_id,
            "w3c_trace_id": self.w3c_trace_id,
            "root_span_id": self.root_span_id,
            "traceparent": self.traceparent,
            "tool": self.tool_name,
            "timestamp": self.timestamp,
            "outcome": outcome,
            "total_ms": total_ms,
            "spans": [s.to_dict() for s in self._spans],
        }
        if self._committed:
            return record
        self._committed = True
        try:
            with _TRACE_FILE_LOCK:
                with open(TRACES_FILE, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record) + "\n")
                # Enforce bounded ring-buffer retention if file exceeds MAX_TRACE_RECORDS
                if MAX_TRACE_RECORDS > 0 and os.path.exists(TRACES_FILE):
                    with open(TRACES_FILE, "r", encoding="utf-8") as rf:
                        lines = [ln for ln in rf if ln.strip()]
                    if len(lines) > MAX_TRACE_RECORDS:
                        kept = lines[-MAX_TRACE_RECORDS:]
                        tmp_path = f"{TRACES_FILE}.tmp"
                        with open(tmp_path, "w", encoding="utf-8") as wf:
                            wf.writelines(kept)
                        os.replace(tmp_path, TRACES_FILE)
        except Exception as e:
            logger.warning(f"[Tracer] Failed to write trace: {e}")
        return record


def get_or_create_trace(tool_name: str) -> tuple[Trace, bool]:
    """
    Return `(active_trace, is_owner)`.
    If a server-level middleware trace is already active in this context,
    reuse it so inner tool spans attach to the same trace without double-committing.
    """
    existing = _ACTIVE_TRACE.get()
    if existing is not None:
        return existing, False
    new_trace = Trace(tool_name)
    _ACTIVE_TRACE.set(new_trace)
    return new_trace, True


def clear_active_trace() -> None:
    """Clear the context-local active trace."""
    _ACTIVE_TRACE.set(None)


def read_recent_traces(limit: int = 10) -> List[Dict[str, Any]]:
    """Return the most recent `limit` traces from traces.jsonl (newest first)."""
    if not os.path.exists(TRACES_FILE):
        return []
    try:
        with _TRACE_FILE_LOCK:
            with open(TRACES_FILE, "r", encoding="utf-8") as f:
                lines = [line.strip() for line in f if line.strip()]
        records = [json.loads(line) for line in lines]
        return list(reversed(records))[:limit]
    except Exception as e:
        logger.warning(f"[Tracer] Failed to read traces: {e}")
        return []


def get_trace_by_id(trace_id: str) -> Optional[Dict[str, Any]]:
    """Lookup a single trace record by trace_id or w3c_trace_id (newest match first)."""
    target = (trace_id or "").strip()
    if not target or not os.path.exists(TRACES_FILE):
        return None
    try:
        with _TRACE_FILE_LOCK:
            with open(TRACES_FILE, "r", encoding="utf-8") as f:
                lines = [line.strip() for line in f if line.strip()]
        for line in reversed(lines):
            record = json.loads(line)
            if record.get("trace_id") == target or record.get("w3c_trace_id") == target:
                return record
    except Exception as e:
        logger.warning(f"[Tracer] Failed to find trace {target}: {e}")
    return None


def get_telemetry_summary(records: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """
    Compute live SLO & Guardrail Telemetry Diagnostics across trace records:
      - p50 / p95 / max / avg latency (ms)
      - success_rate_pct, error_rate_pct, guardrail_interceptions
      - per_tool_breakdown ({calls, avg_ms, errors, guardrail_blocks})
      - slo_status ("healthy" | "degraded")
    """
    if records is None:
        records = read_recent_traces(limit=200)

    total = len(records)
    if total == 0:
        return {
            "total_traces": 0,
            "p50_latency_ms": 0.0,
            "p95_latency_ms": 0.0,
            "max_latency_ms": 0.0,
            "avg_latency_ms": 0.0,
            "success_rate_pct": 100.0,
            "error_rate_pct": 0.0,
            "guardrail_interceptions": 0,
            "per_tool_breakdown": {},
            "slo_status": "healthy",
        }

    latencies = sorted(float(r.get("total_ms", 0.0)) for r in records)
    p50_idx = min(total - 1, int(round(0.50 * (total - 1))))
    p95_idx = min(total - 1, int(round(0.95 * (total - 1))))
    p50_ms = round(latencies[p50_idx], 2)
    p95_ms = round(latencies[p95_idx], 2)
    max_ms = round(latencies[-1], 2)
    avg_ms = round(sum(latencies) / total, 2)

    error_outcomes = {"error", "exception", "schema_validation_error"}
    guardrail_outcomes = {"policy_denied", "duplicate_blocked", "confirmation_required", "blocked"}

    errors = 0
    guardrail_blocks = 0
    per_tool: Dict[str, Dict[str, Any]] = {}

    for r in records:
        tool = str(r.get("tool", "unknown"))
        outcome = str(r.get("outcome", "success")).lower()
        dur = float(r.get("total_ms", 0.0))

        # Also inspect inner spans for policy/dedup blocks
        span_statuses = {str(s.get("status", "")).lower() for s in r.get("spans", []) if isinstance(s, dict)}

        is_err = outcome in error_outcomes or "error" in span_statuses
        is_guardrail = (
            outcome in guardrail_outcomes
            or bool(span_statuses & {"blocked", "policy_denied", "duplicate_blocked"})
        )

        if is_err:
            errors += 1
        if is_guardrail:
            guardrail_blocks += 1

        bucket = per_tool.setdefault(
            tool, {"calls": 0, "total_ms": 0.0, "errors": 0, "guardrail_blocks": 0}
        )
        bucket["calls"] += 1
        bucket["total_ms"] += dur
        if is_err:
            bucket["errors"] += 1
        if is_guardrail:
            bucket["guardrail_blocks"] += 1

    per_tool_summary = {
        t: {
            "calls": b["calls"],
            "avg_ms": round(b["total_ms"] / b["calls"], 2),
            "errors": b["errors"],
            "guardrail_blocks": b["guardrail_blocks"],
        }
        for t, b in per_tool.items()
    }

    error_rate_pct = round((errors / total) * 100.0, 2)
    success_rate_pct = round(100.0 - error_rate_pct, 2)
    slo_status = "healthy" if (error_rate_pct < 10.0 and p95_ms < 2000.0) else "degraded"

    return {
        "total_traces": total,
        "p50_latency_ms": p50_ms,
        "p95_latency_ms": p95_ms,
        "max_latency_ms": max_ms,
        "avg_latency_ms": avg_ms,
        "success_rate_pct": success_rate_pct,
        "error_rate_pct": error_rate_pct,
        "guardrail_interceptions": guardrail_blocks,
        "per_tool_breakdown": per_tool_summary,
        "slo_status": slo_status,
    }


def parse_w3c_traceparent(header: Optional[str]) -> Optional[Dict[str, str]]:
    """
    Validate and parse a W3C Trace Context `traceparent` header:
        `00-<32-hex-trace-id>-<16-hex-parent-id>-<2-hex-flags>`
    Returns `{"version": "00", "trace_id": ..., "parent_id": ..., "flags": ...}` or `None`.
    """
    import re

    if not header or not isinstance(header, str):
        return None
    m = re.match(
        r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$",
        header.strip().lower(),
    )
    if not m:
        return None
    version, trace_id, parent_id, flags = m.groups()
    if trace_id == "0" * 32 or parent_id == "0" * 16:
        return None
    return {
        "version": version,
        "trace_id": trace_id,
        "parent_id": parent_id,
        "flags": flags,
    }


def export_otlp_spans(
    records: Optional[List[Dict[str, Any]]] = None,
    limit: int = 20,
    service_name: str = "github-mcp-toolkit",
) -> Dict[str, Any]:
    """
    Export recorded execution traces in the official OpenTelemetry OTLP/JSON
    `resourceSpans` schema (`v1/traces`) for direct ingestion into Datadog,
    Grafana Tempo, Jaeger, or Honeycomb.
    """
    if records is None:
        records = read_recent_traces(limit=limit)

    otlp_spans: List[Dict[str, Any]] = []
    base_ns = time.time_ns()

    for rec in records:
        w3c_tid = rec.get("w3c_trace_id") or uuid.uuid4().hex
        root_sid = rec.get("root_span_id") or uuid.uuid4().hex[:16]
        total_ns = int(float(rec.get("total_ms", 1.0)) * 1_000_000)
        outcome = str(rec.get("outcome", "success"))
        tool_name = str(rec.get("tool", "unknown_tool"))

        # Root tool invocation span
        otlp_spans.append({
            "traceId": w3c_tid,
            "spanId": root_sid,
            "name": f"mcp.tool.{tool_name}",
            "kind": 2,  # SPAN_KIND_SERVER
            "startTimeUnixNano": str(base_ns - total_ns),
            "endTimeUnixNano": str(base_ns),
            "status": {
                "code": 1 if outcome in {"success", "completed", "created", "closed", "reverted"} else 2,
                "message": outcome,
            },
            "attributes": [
                {"key": "mcp.tool.name", "value": {"stringValue": tool_name}},
                {"key": "mcp.trace.id", "value": {"stringValue": str(rec.get("trace_id", ""))}},
                {"key": "w3c.traceparent", "value": {"stringValue": str(rec.get("traceparent", ""))}},
            ],
        })

        # Child phase spans
        for child in rec.get("spans", []):
            if not isinstance(child, dict):
                continue
            c_sid = child.get("span_id") or uuid.uuid4().hex[:16]
            c_dur_ns = int(float(child.get("duration_ms") or 0.1) * 1_000_000)
            c_status = str(child.get("status", "ok"))
            otlp_spans.append({
                "traceId": w3c_tid,
                "spanId": c_sid,
                "parentSpanId": child.get("parent_span_id") or root_sid,
                "name": f"mcp.phase.{child.get('span', 'phase')}",
                "kind": 1,  # SPAN_KIND_INTERNAL
                "startTimeUnixNano": str(base_ns - c_dur_ns),
                "endTimeUnixNano": str(base_ns),
                "status": {
                    "code": 1 if c_status in {"ok", "valid", "allowed"} else 2,
                    "message": c_status,
                },
                "attributes": [
                    {"key": "mcp.phase.status", "value": {"stringValue": c_status}},
                ],
            })

    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": service_name}},
                        {"key": "telemetry.sdk.language", "value": {"stringValue": "python"}},
                        {"key": "mcp.spec.version", "value": {"stringValue": "1.0.0"}},
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {
                            "name": "github_mcp_toolkit.tracer",
                            "version": "3.0.0",
                        },
                        "spans": otlp_spans,
                    }
                ],
            }
        ]
    }

