from typing import Dict, Any, Optional
from policy_engine import PolicyEngine
from transaction_journal import TransactionJournal


def register(mcp, gh):
    @mcp.tool()
    def close_issue(repo_name: str, issue_number: int, comment: str = "", confirmed: bool = False) -> Dict[str, Any]:
        """
        Close an existing open GitHub issue, optionally leaving a final comment.
        
        GUARDRAILS:
        - ABAC Policy Engine evaluation (write-freeze check).
        - Saga Pattern transaction recording for undo_last_action rollback (reopen).
        
        IMPORTANT: Closing an issue alters state.
        Only call with confirmed=True AFTER user approval.
        """
        if not repo_name or issue_number <= 0:
            return {
                "status": "error",
                "type": "invalid_argument",
                "message": "repo_name and positive issue_number are required."
            }

        # ── ABAC Policy Check ──────────────────────────────────────────────
        allowed, reason = PolicyEngine.evaluate(
            "close_issue", {"repo_name": repo_name, "issue_number": issue_number}
        )
        if not allowed:
            return {"status": "policy_denied", "reason": reason}

        if not confirmed:
            return {
                "status": "confirmation_required",
                "action": "close_issue",
                "repo_name": repo_name,
                "issue_number": issue_number,
                "comment": comment,
                "message": f"Confirmation Required: Close issue #{issue_number} in '{repo_name}'? Please confirm with confirmed=True."
            }

        try:
            result = gh.close_issue(repo_name.strip(), issue_number, comment.strip() if comment else None)

            # ── Saga Journal Entry ─────────────────────────────────────────
            if result.get("status") == "closed":
                tx_id = TransactionJournal.record_transaction(
                    tool="close_issue",
                    repo_name=repo_name.strip(),
                    parameters={"issue_number": issue_number, "comment": comment},
                    compensation={"type": "reopen_issue", "issue_number": issue_number},
                )
                result["transaction_id"] = tx_id

            return result
        except Exception as e:
            return {
                "status": "error",
                "type": "github_api_error",
                "message": str(e)
            }
