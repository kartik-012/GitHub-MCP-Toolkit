"""
Pydantic Response Schemas — GitHub MCP Toolkit
================================================
Every MCP tool return value is validated against one of these schemas
before being handed back to the LLM. This enforces an explicit API contract
between the tool layer and the LLM consumer, preventing:

  - Silent schema drift when the GitHub API response shape changes
  - LLMs receiving partial/malformed data and producing hallucinated completions
  - Runtime KeyError bugs in downstream tool-chain calls

Usage pattern (inside any tool):
    from schemas import validate, IssueListResponse
    return validate(IssueListResponse, raw_dict)

`validate()` returns the model as a plain dict (LLM-serialisable).
On validation failure it returns a structured SchemaValidationError dict
instead of crashing the server.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Type
from pydantic import BaseModel, ValidationError, field_validator


# ---------------------------------------------------------------------------
# Shared primitives
# ---------------------------------------------------------------------------

class IssueSummary(BaseModel):
    repo: str
    number: int
    title: str
    state: str
    labels: List[str] = []
    url: str
    updated_at: Optional[str] = None


class SpanRecord(BaseModel):
    span: str
    duration_ms: Optional[float] = None
    status: str
    meta: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Tool-specific schemas
# ---------------------------------------------------------------------------

class IssueListResponse(BaseModel):
    status: str
    total: int
    issues: List[IssueSummary]


class SearchResponse(BaseModel):
    status: str
    keyword: str
    total_matched: int
    results: List[IssueSummary]


class CreateIssueResponse(BaseModel):
    status: str           # created | already_exists | confirmation_required | potential_duplicate_found | policy_denied | error
    repo: Optional[str] = None
    number: Optional[int] = None
    title: Optional[str] = None
    url: Optional[str] = None
    message: Optional[str] = None
    transaction_id: Optional[str] = None
    similarity_score: Optional[float] = None
    existing_issue: Optional[Dict[str, Any]] = None
    reason: Optional[str] = None


class AddLabelResponse(BaseModel):
    status: str
    repo: Optional[str] = None
    issue_number: Optional[int] = None
    label_added: Optional[str] = None
    current_labels: Optional[List[str]] = None
    message: Optional[str] = None


class CloseIssueResponse(BaseModel):
    status: str
    repo: Optional[str] = None
    issue_number: Optional[int] = None
    state: Optional[str] = None
    message: Optional[str] = None


class BulkLabelResponse(BaseModel):
    status: str
    preview_token: Optional[str] = None
    affected_count: Optional[int] = None
    affected_issues: Optional[List[Dict[str, Any]]] = None
    label_to_apply: Optional[str] = None
    total_affected: Optional[int] = None
    results: Optional[List[Dict[str, Any]]] = None
    message: Optional[str] = None


class TriageResponse(BaseModel):
    status: str
    repo_name: Optional[str] = None
    issue_number: Optional[int] = None
    title: Optional[str] = None
    classification: Optional[Dict[str, Any]] = None
    applied_labels: Optional[List[str]] = None
    message: Optional[str] = None


class RateLimitResponse(BaseModel):
    limit: int
    remaining: int
    used: int
    reset_timestamp: Optional[str] = None


class SemanticSearchResponse(BaseModel):
    status: str
    query: str
    total_matched: int
    results: List[Dict[str, Any]]


class UndoResponse(BaseModel):
    status: str
    tx_id: Optional[str] = None
    original_tool: Optional[str] = None
    compensation_result: Optional[Dict[str, Any]] = None
    message: Optional[str] = None


class TransactionHistoryResponse(BaseModel):
    status: str
    total_records: int
    history: List[Dict[str, Any]]


class TraceHistoryResponse(BaseModel):
    status: str
    total_records: int
    traces: List[Dict[str, Any]]


# ---------------------------------------------------------------------------
# Schema validation helper
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
