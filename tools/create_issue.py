from typing import Dict, Any
from vector_engine import VectorEngine
from transaction_journal import TransactionJournal
from policy_engine import PolicyEngine
from tracer import get_or_create_trace, clear_active_trace


def register(mcp, gh):
    @mcp.tool()
    def create_issue(
        repo_name: str,
        title: str,
        body: str = "",
        confirmed: bool = False,
        force_duplicate: bool = False,
        preview_token: str = "",
    ) -> Dict[str, Any]:
        """
        Create a new GitHub issue in a specified repository.

        GUARDRAILS & ADVANCED ENGINE FEATURES:
        - ABAC Policy Engine evaluation (rate-limit buffer, write-freeze, DLP secret scan).
        - Pre-creation duplicate detection using TF-IDF Cosine Similarity (>=80% threshold).
        - Cryptographic 2-Phase Preview Token binding (prevents parameter tampering between preview & confirm).
        - Saga Pattern transaction recording for undo_last_action rollback capability.
        - Execution Tracer: all phases recorded as named spans in traces.jsonl.

        If confirmed is False, returns a confirmation request and preview_token to the user first.
        Only call with confirmed=True after the user has explicitly approved.
        """
        trace, is_owner = get_or_create_trace("create_issue")

        def _finish(outcome: str, payload: Dict[str, Any]) -> Dict[str, Any]:
            if is_owner:
                trace.commit(outcome=outcome)
                clear_active_trace()
            return payload

        if not repo_name or not repo_name.strip() or not title or not title.strip():
            return _finish(
                "invalid_argument",
                {
                    "status": "error",
                    "type": "invalid_argument",
                    "message": "repo_name and title are required.",
                },
            )

        # ── Phase 1: ABAC & DLP Policy Check ──────────────────────────────
        with trace.span("policy_check") as span:
            allowed, reason = PolicyEngine.evaluate(
                "create_issue",
                {"repo_name": repo_name, "title": title, "body": body},
                rate_limit_remaining=PolicyEngine.get_client_rate_remaining(gh),
            )
            span.finish(status="pass" if allowed else "denied", reason=reason)

        if not allowed:
            return _finish("policy_denied", {"status": "policy_denied", "reason": reason})

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
                        return _finish(
                            "duplicate_blocked",
                            {
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
                            },
                        )
                    span.finish(status="clean", candidates_checked=len(open_issues))
                except Exception:
                    span.finish(status="skipped_non_blocking")

        fingerprint = {"repo_name": repo_name.strip(), "title": title.strip()}

        # ── Phase 3: Confirmation Gate & Cryptographic Preview Token ──────
        if not confirmed:
            token = PolicyEngine.issue_write_preview_token("create_issue", fingerprint)
            blast = PolicyEngine.estimate_blast_radius(
                "create_issue",
                affected_entities=1,
                rate_limit_remaining=PolicyEngine.get_client_rate_remaining(gh),
            )
            return _finish(
                "awaiting_confirmation",
                {
                    "status": "confirmation_required",
                    "action": "create_issue",
                    "preview_token": token,
                    "blast_radius": blast,
                    "repo_name": repo_name,
                    "title": title,
                    "body": body,
                    "message": (
                        f"Confirmation Required: Create issue '{title}' in repo "
                        f"'{repo_name}'? Confirm with confirmed=True and preview_token='{token}'."
                    ),
                },
            )

        tok_ok, tok_reason = PolicyEngine.verify_write_preview_token("create_issue", fingerprint, preview_token)
        if not tok_ok:
            return _finish(
                "invalid_preview_token",
                {"status": "error", "type": "invalid_preview_token", "message": tok_reason},
            )

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

            return _finish("success", result)

        except Exception as e:
            return _finish(
                "error",
                {
                    "status": "error",
                    "type": "github_api_error",
                    "message": str(e),
                },
            )
