from typing import Dict, Any
from policy_engine import PolicyEngine
from transaction_journal import TransactionJournal


def register(mcp, gh):
    @mcp.tool()
    def add_label(
        repo_name: str,
        issue_number: int,
        label: str,
        confirmed: bool = False,
        preview_token: str = "",
    ) -> Dict[str, Any]:
        """
        Add a label to an existing GitHub issue.
        
        GUARDRAILS:
        - ABAC Policy Engine evaluation (restricted label check, write-freeze).
        - Cryptographic 2-Phase Preview Token binding.
        - Saga Pattern transaction recording for undo_last_action rollback.
        
        IMPORTANT: This tool modifies the user's repository state.
        Only call with confirmed=True AFTER the user has explicitly approved adding 
        label '{label}' to issue #{issue_number} in repo '{repo_name}'.
        
        If confirmed is False, return a confirmation request message first.
        """
        if not repo_name or not label or issue_number <= 0:
            return {
                "status": "error",
                "type": "invalid_argument",
                "message": "repo_name, positive issue_number, and label are required."
            }

        # ── ABAC Policy Check ──────────────────────────────────────────────
        allowed, reason = PolicyEngine.evaluate(
            "add_label",
            {"repo_name": repo_name, "label": label},
            rate_limit_remaining=PolicyEngine.get_client_rate_remaining(gh),
        )
        if not allowed:
            return {"status": "policy_denied", "reason": reason}

        fingerprint = {"repo_name": repo_name.strip(), "issue_number": issue_number, "label": label.strip()}

        if not confirmed:
            token = PolicyEngine.issue_write_preview_token("add_label", fingerprint)
            blast = PolicyEngine.estimate_blast_radius(
                "add_label",
                affected_entities=1,
                rate_limit_remaining=PolicyEngine.get_client_rate_remaining(gh),
            )
            return {
                "status": "confirmation_required",
                "action": "add_label",
                "preview_token": token,
                "blast_radius": blast,
                "repo_name": repo_name,
                "issue_number": issue_number,
                "label": label,
                "message": (
                    f"Confirmation Required: Add label '{label}' to issue #{issue_number} in '{repo_name}'? "
                    f"Please confirm with confirmed=True and preview_token='{token}'."
                ),
            }

        tok_ok, tok_reason = PolicyEngine.verify_write_preview_token("add_label", fingerprint, preview_token)
        if not tok_ok:
            return {"status": "error", "type": "invalid_preview_token", "message": tok_reason}

        try:
            result = gh.add_label(repo_name.strip(), issue_number, label.strip())

            # ── Saga Journal Entry ─────────────────────────────────────────
            if result.get("status") == "success":
                tx_id = TransactionJournal.record_transaction(
                    tool="add_label",
                    repo_name=repo_name.strip(),
                    parameters={"issue_number": issue_number, "label": label.strip()},
                    compensation={"type": "remove_label", "issue_number": issue_number, "label": label.strip()},
                )
                result["transaction_id"] = tx_id

            return result
        except Exception as e:
            return {
                "status": "error",
                "type": "github_api_error",
                "message": str(e)
            }
