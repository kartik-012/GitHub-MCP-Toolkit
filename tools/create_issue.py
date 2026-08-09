from typing import Dict, Any
from vector_engine import VectorEngine
from transaction_journal import TransactionJournal
from policy_engine import PolicyEngine
from tracer import Trace
from schemas import validate, CreateIssueResponse


def register(mcp, gh):
    @mcp.tool()
    def create_issue(
        repo_name: str,
        title: str,
        body: str = "",
        confirmed: bool = False,
        force_duplicate: bool = False
    ) -> Dict[str, Any]:
        """
        Create a new GitHub issue in a specified repository.

        GUARDRAILS & ADVANCED ENGINE FEATURES:
        - ABAC Policy Engine evaluation (rate-limit buffer, write-freeze, label restrictions).
        - Pre-creation duplicate detection using Vector Cosine Similarity (≥80% threshold).
        - Saga Pattern transaction recording for undo_last_action rollback capability.
        - Execution Tracer: all phases recorded as named spans in traces.jsonl.

        If confirmed is False, returns a confirmation request to the user first.
        Only call with confirmed=True after the user has explicitly approved.
        """
        trace = Trace("create_issue")

        if not repo_name or not repo_name.strip() or not title or not title.strip():
            return {
                "status": "error",
                "type": "invalid_argument",
                "message": "repo_name and title are required.",
            }

        # ── Phase 1: ABAC Policy Check ────────────────────────────────────
        with trace.span("policy_check") as span:
            allowed, reason = PolicyEngine.evaluate(
                "create_issue", {"repo_name": repo_name, "title": title}
            )
            span.finish(status="pass" if allowed else "denied", reason=reason)

        if not allowed:
            trace.commit(outcome="policy_denied")
            return {"status": "policy_denied", "reason": reason}

        # ── Phase 2: Vector Duplicate Detection ───────────────────────────
        if not force_duplicate:
            with trace.span("vector_dedup") as span:
                try:
                    open_issues = gh.list_open_issues(repo_name.strip())
                    ranked = VectorEngine.rank_documents(
                        f"{title} {body}", open_issues, top_k=1
                    )
                    if ranked and ranked[0]["similarity_score"] >= 0.80:
                        dup = ranked[0]
                        span.finish(
                            status="duplicate_found",
                            score=dup["similarity_score"],
                            existing_issue_number=dup["number"],
                        )
                        trace.commit(outcome="duplicate_blocked")
                        return {
                            "status": "potential_duplicate_found",
                            "similarity_score": dup["similarity_score"],
                            "existing_issue": {
                                "number": dup["number"],
                                "title": dup["title"],
                                "url": dup["url"],
                            },
                            "message": (
                                f"Warning: Found existing issue #{dup['number']} "
                                f"('{dup['title']}') with "
                                f"{int(dup['similarity_score'] * 100)}% semantic similarity. "
                                f"To force creation, call again with force_duplicate=True AND confirmed=True."
                            ),
                        }
                    span.finish(status="clean", candidates_checked=len(open_issues))
                except Exception:
                    span.finish(status="skipped_non_blocking")

        # ── Phase 3: Confirmation Gate ────────────────────────────────────
        if not confirmed:
            trace.commit(outcome="awaiting_confirmation")
            return {
                "status": "confirmation_required",
                "action": "create_issue",
                "repo_name": repo_name,
                "title": title,
                "body": body,
                "message": (
                    f"Confirmation Required: Create issue '{title}' in repo "
                    f"'{repo_name}'? Confirm with confirmed=True."
                ),
            }

        # ── Phase 4: GitHub API Call ──────────────────────────────────────
        try:
            with trace.span("github_api") as span:
                result = gh.create_issue(repo_name.strip(), title.strip(), body)
                span.finish(status=result.get("status", "unknown"))

            # ── Phase 5: Saga Journal Entry ───────────────────────────────
            if result.get("status") == "created":
                with trace.span("saga_journal") as span:
                    issue_num = result["number"]
                    tx_id = TransactionJournal.record_transaction(
                        tool="create_issue",
                        repo_name=repo_name.strip(),
                        parameters={"title": title, "body": body},
                        compensation={"type": "close_issue", "issue_number": issue_num},
                    )
                    span.finish(status="committed", tx_id=tx_id)
                result["transaction_id"] = tx_id

            trace.commit(outcome="success")
            return validate(CreateIssueResponse, result)

        except Exception as e:
            trace.commit(outcome="error")
            return {
                "status": "error",
                "type": "github_api_error",
                "message": str(e),
            }
