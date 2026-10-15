from typing import Dict, Any
from tracer import read_recent_traces, get_telemetry_summary, export_otlp_spans


def register(mcp, gh):
    @mcp.tool()
    def get_trace_history(limit: int = 10, export_otlp: bool = False) -> Dict[str, Any]:
        """
        Return the most recent tool execution traces showing per-phase timing,
        W3C `traceparent` headers, live SLO & Guardrail telemetry diagnostics,
        and optional OpenTelemetry OTLP/JSON (`resourceSpans`) export.
        """
        if limit < 1 or limit > 100:
            return {
                "status": "error",
                "type": "invalid_argument",
                "message": "limit must be between 1 and 100.",
            }
        try:
            traces = read_recent_traces(limit=limit)
            slo_summary = get_telemetry_summary(traces)
            res: Dict[str, Any] = {
                "status": "success",
                "total_records": len(traces),
                "slo_summary": slo_summary,
                "traces": traces,
            }
            if export_otlp:
                res["otlp_export"] = export_otlp_spans(records=traces, limit=limit)
            return res
        except Exception as e:
            return {
                "status": "error",
                "type": "trace_read_failed",
                "message": str(e),
            }

