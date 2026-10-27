"""
Pydantic Response Schemas — GitHub MCP Toolkit
================================================
Every MCP tool return value is validated against one of these schemas
before being handed back to the LLM. This enforces an explicit API contract
between the tool layer and the LLM consumer, preventing:

  - Silent schema drift when the GitHub API response shape changes
  - LLMs receiving partial/malformed data and producing hallucinated completions
  - Runtime KeyError bugs in downstream tool-chain calls
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Type
from pydantic import BaseModel, ConfigDict, ValidationError


class _AllowExtraModel(BaseModel):
    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------------------
# Shared primitives
# ---------------------------------------------------------------------------

class IssueSummary(_AllowExtraModel):
    """Standardized summary representation of a GitHub repository issue."""
    repo: str
    number: int
    title: str
    state: str
    labels: List[str] = []
    url: str
    updated_at: Optional[str] = None


class SpanRecord(_AllowExtraModel):
    """Execution span telemetric record capturing duration and execution status."""
    span: str
    duration_ms: Optional[float] = None
    status: str
    meta: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Tool-specific schemas
# ---------------------------------------------------------------------------

class IssueListResponse(_AllowExtraModel):
    """Response payload returned when querying an issue list from a repository."""
    status: str
    total: Optional[int] = None
    issues: Optional[List[IssueSummary]] = None


class ListRepositoriesResponse(_AllowExtraModel):
    """Response payload returned when querying accessible repositories."""
    status: str
    total: Optional[int] = None
    repositories: Optional[List[Dict[str, Any]]] = None


class SearchResponse(_AllowExtraModel):
    """Response payload containing matching issues from keyword or semantic search."""
    status: str
    keyword: Optional[str] = None
    total_matched: Optional[int] = None
    results: Optional[List[IssueSummary]] = None


class CreateIssueResponse(_AllowExtraModel):
    status: str
    repo: Optional[str] = None
    number: Optional[int] = None
    title: Optional[str] = None
    url: Optional[str] = None
    message: Optional[str] = None
    transaction_id: Optional[str] = None
    similarity_score: Optional[float] = None
    existing_issue: Optional[Dict[str, Any]] = None
    reason: Optional[str] = None


class AddLabelResponse(_AllowExtraModel):
    status: str
    repo: Optional[str] = None
    issue_number: Optional[int] = None
    label_added: Optional[str] = None
    current_labels: Optional[List[str]] = None
    message: Optional[str] = None


class CloseIssueResponse(_AllowExtraModel):
    status: str
    repo: Optional[str] = None
    issue_number: Optional[int] = None
    state: Optional[str] = None
    message: Optional[str] = None


class BulkLabelResponse(_AllowExtraModel):
    status: str
    preview_token: Optional[str] = None
    affected_count: Optional[int] = None
    affected_issues: Optional[List[Dict[str, Any]]] = None
    label_to_apply: Optional[str] = None
    total_affected: Optional[int] = None
    results: Optional[List[Dict[str, Any]]] = None
    message: Optional[str] = None


class TriageResponse(_AllowExtraModel):
    status: str
    repo_name: Optional[str] = None
    issue_number: Optional[int] = None
    title: Optional[str] = None
    classification: Optional[Dict[str, Any]] = None
    applied_labels: Optional[List[str]] = None
    message: Optional[str] = None


class RateLimitResponse(_AllowExtraModel):
    limit: Optional[int] = None
    remaining: Optional[int] = None
    used: Optional[int] = None
    reset_timestamp: Optional[str] = None
    status: Optional[str] = None


class SemanticSearchResponse(_AllowExtraModel):
    status: str
    query: Optional[str] = None
    total_matched: Optional[int] = None
    results: Optional[List[Dict[str, Any]]] = None


class UndoResponse(_AllowExtraModel):
    status: str
    tx_id: Optional[str] = None
    original_tool: Optional[str] = None
    compensation_result: Optional[Dict[str, Any]] = None
    message: Optional[str] = None


class TransactionHistoryResponse(_AllowExtraModel):
    status: str
    total_records: Optional[int] = None
    history: Optional[List[Dict[str, Any]]] = None


class TraceHistoryResponse(_AllowExtraModel):
    status: str
    total_records: Optional[int] = None
    traces: Optional[List[Dict[str, Any]]] = None


TOOL_SCHEMA_MAP: Dict[str, Type[BaseModel]] = {
    "get_open_issues": IssueListResponse,
    "list_repositories": ListRepositoriesResponse,
    "search_issues": SearchResponse,
    "create_issue": CreateIssueResponse,
    "add_label": AddLabelResponse,
    "close_issue": CloseIssueResponse,
    "bulk_label_stale_issues": BulkLabelResponse,
    "triage_issue": TriageResponse,
    "get_rate_limit_status": RateLimitResponse,
    "semantic_search_issues": SemanticSearchResponse,
    "undo_last_action": UndoResponse,
    "get_transaction_history": TransactionHistoryResponse,
    "get_trace_history": TraceHistoryResponse,
}


# ---------------------------------------------------------------------------
# Schema validation helpers
# ---------------------------------------------------------------------------

def validate(schema: Type[BaseModel], data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validate `data` against `schema`.
    Returns the validated model as a plain dict on success.
    Returns a structured SchemaValidationError dict on failure (never raises).
    """
    try:
        return schema.model_validate(data).model_dump(exclude_none=True)
    except ValidationError as exc:
        return {
            "status": "schema_validation_error",
            "schema": schema.__name__,
            "errors": exc.errors(include_url=False),
            "raw_data": data,
        }


def estimate_tokens(payload: Any) -> int:
    """
    Estimate LLM BPE token count for a Python object using the ~4 chars/token heuristic.
    """
    import json
    import math

    try:
        serialized = json.dumps(payload, default=str)
    except Exception:
        serialized = str(payload)
    return max(1, math.ceil(len(serialized) / 4))


def compact_and_budget_payload(
    data: Any,
    max_tokens: int = 3500,
    max_field_chars: int = 600,
) -> Any:
    """
    Context-Window Token Budgeter & Adaptive Payload Compactor.
    Prevents verbose GitHub issue bodies or large result lists from overflowing
    the LLM context window ("lost in the middle" degradation & token cost spikes).
    """
    if not isinstance(data, dict):
        return data

    before_tokens = estimate_tokens(data)
    compacted = False
    out = dict(data)

    # Compact oversized free-text `body` fields inside nested issue lists (`issues`, `results`)
    for list_key in ("issues", "results", "affected_issues"):
        items = out.get(list_key)
        if isinstance(items, list):
            new_items = []
            for item in items:
                if isinstance(item, dict) and isinstance(item.get("body"), str):
                    body_str = item["body"]
                    if len(body_str) > max_field_chars:
                        item_copy = dict(item)
                        item_copy["body"] = (
                            body_str[:max_field_chars] + " ...[compacted for context budget]"
                        )
                        new_items.append(item_copy)
                        compacted = True
                    else:
                        new_items.append(item)
                else:
                    new_items.append(item)
            out[list_key] = new_items

    # Also compact top-level `body` if oversized
    if isinstance(out.get("body"), str) and len(out["body"]) > max_field_chars:
        out["body"] = out["body"][:max_field_chars] + " ...[compacted for context budget]"
        compacted = True

    after_tokens = estimate_tokens(out)
    if compacted or before_tokens > max_tokens:
        out["_context_budget"] = {
            "before_tokens": before_tokens,
            "after_tokens": after_tokens,
            "tokens_saved": max(0, before_tokens - after_tokens),
            "budget_limit_tokens": max_tokens,
            "compacted": compacted,
        }
    return out


def validate_tool_output(tool_name: str, result: Any) -> Any:
    """
    Automatically validate dict tool outputs against TOOL_SCHEMA_MAP
    and enforce Context-Window Token Budgeting.
    List returns (such as raw issue lists) pass through unchanged.
    """
    if not isinstance(result, dict):
        return result
    compacted = compact_and_budget_payload(result)
    schema = TOOL_SCHEMA_MAP.get(tool_name)
    if schema is None:
        return compacted
    return validate(schema, compacted)


def is_valid_payload(schema_cls: Type[BaseModel], data: Any) -> bool:
    """
    Check if data conforms to a given Pydantic schema without raising ValidationError.
    Returns True if valid, False otherwise.
    """
    if not isinstance(data, dict):
        return False
    try:
        schema_cls.model_validate(data)
        return True
    except (ValidationError, Exception):
        return False

