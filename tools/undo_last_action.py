from typing import Dict, Any
from transaction_journal import TransactionJournal


def register(mcp, gh):
    @mcp.tool()
    def undo_last_action(confirmed: bool = False) -> Dict[str, Any]:
        """
        Roll back the most recent committed write mutation using Saga pattern compensation.
        
        IMPORTANT GUARDRAIL: Only call with confirmed=True AFTER user explicit approval 
        to revert the last action.
        """
        last_tx = TransactionJournal.get_last_committed_transaction()
        if not last_tx:
            return {
                "status": "no_action",
                "message": "No active committed write transaction found to undo."
            }

        tool_name = last_tx["tool"]
        compensation = last_tx["compensation"]
        tx_id = last_tx["tx_id"]

        if not confirmed:
            return {
                "status": "confirmation_required",
                "action": "undo_last_action",
                "tx_id": tx_id,
                "target_tool": tool_name,
                "compensation": compensation,
                "message": (
                    f"Confirmation Required: Undo last action '{tool_name}' (Transaction ID: {tx_id})? "
                    f"This will execute compensation action: {compensation}. Confirm with confirmed=True."
                )
            }

        try:
            comp_type = compensation.get("type")
            repo = last_tx["repo_name"]

            if comp_type == "close_issue":
                res = gh.close_issue(repo, compensation["issue_number"], comment="Reverted via undo_last_action.")
            elif comp_type == "reopen_issue":
                # PyGithub reopening issue
                user = gh._get_authenticated_user()
                target_repo = f"{user.login}/{repo}" if "/" not in repo else repo
                issue_obj = gh.gh.get_repo(target_repo).get_issue(compensation["issue_number"])
                issue_obj.edit(state="open")
                res = {"status": "reopened", "issue_number": compensation["issue_number"]}
            elif comp_type == "remove_label":
                user = gh._get_authenticated_user()
                target_repo = f"{user.login}/{repo}" if "/" not in repo else repo
                issue_obj = gh.gh.get_repo(target_repo).get_issue(compensation["issue_number"])
                issue_obj.remove_from_labels(compensation["label"])
                res = {"status": "label_removed", "label": compensation["label"]}
            else:
                return {
                    "status": "error",
                    "type": "unsupported_compensation",
                    "message": f"Compensation type '{comp_type}' is not supported."
                }

            TransactionJournal.mark_reverted(tx_id)
            return {
                "status": "reverted",
                "tx_id": tx_id,
                "original_tool": tool_name,
                "compensation_result": res
            }

        except Exception as e:
            return {
                "status": "error",
                "type": "rollback_failed",
                "message": f"Failed to revert transaction {tx_id}: {str(e)}"
            }
