from typing import Dict, Any
from transaction_journal import TransactionJournal


def _build_compensation_plan(repo: str, compensation: Dict[str, Any]) -> Dict[str, Any]:
    """Build a human- and agent-readable dry-run plan for a Saga compensation."""
    comp_type = compensation.get("type", "unknown")
    if comp_type == "close_issue":
        num = compensation.get("issue_number")
        return {
            "operation": "gh.close_issue",
            "repo_name": repo,
            "target_entities": [f"issue #{num}"],
            "estimated_api_calls": 1,
            "summary": f"Close newly created issue #{num} in '{repo}'.",
        }
    if comp_type == "reopen_issue":
        num = compensation.get("issue_number")
        return {
            "operation": "gh.reopen_issue",
            "repo_name": repo,
            "target_entities": [f"issue #{num}"],
            "estimated_api_calls": 1,
            "summary": f"Reopen closed issue #{num} in '{repo}'.",
        }
    if comp_type == "remove_label":
        num = compensation.get("issue_number")
        lbl = compensation.get("label")
        return {
            "operation": "gh.remove_label",
            "repo_name": repo,
            "target_entities": [f"issue #{num} (label: '{lbl}')"],
            "estimated_api_calls": 1,
            "summary": f"Remove label '{lbl}' from issue #{num} in '{repo}'.",
        }
    if comp_type == "bulk_remove_label":
        nums = compensation.get("issue_numbers", [])
        lbl = compensation.get("label")
        return {
            "operation": "gh.remove_label (batch)",
            "repo_name": repo,
            "target_entities": [f"issue #{n} (label: '{lbl}')" for n in nums],
            "estimated_api_calls": len(nums),
            "summary": f"Remove label '{lbl}' from {len(nums)} issues ({nums}) in '{repo}'.",
        }
    return {
        "operation": f"unsupported:{comp_type}",
        "repo_name": repo,
        "target_entities": [],
        "estimated_api_calls": 0,
        "summary": f"Unsupported compensation type '{comp_type}'.",
    }


def register(mcp, gh):
    @mcp.tool()
    def undo_last_action(
        confirmed: bool = False,
        tx_id: str = "",
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """
        Roll back the most recent committed write mutation (or a specific transaction
        by `tx_id`) using Saga pattern compensation, or preview the rollback plan with
        `dry_run=True`.

        IMPORTANT GUARDRAIL: Only call with confirmed=True AFTER user explicit approval
        to revert the action. Use dry_run=True to inspect the exact compensating API
        mutations without modifying GitHub state.
        """
        requested_tx_id = (tx_id or "").strip()
        if requested_tx_id:
            target_tx = TransactionJournal.get_transaction_by_id(requested_tx_id)
            if not target_tx:
                return {
                    "status": "error",
                    "type": "transaction_not_found",
                    "message": f"Transaction '{requested_tx_id}' was not found in the journal.",
                }
            if target_tx.get("status") == "reverted":
                return {
                    "status": "error",
                    "type": "already_reverted",
                    "tx_id": requested_tx_id,
                    "message": f"Transaction '{requested_tx_id}' has already been reverted.",
                }
        else:
            target_tx = TransactionJournal.get_last_committed_transaction()
            if not target_tx:
                return {
                    "status": "no_action",
                    "message": "No active committed write transaction found to undo.",
                }

        tool_name = target_tx["tool"]
        compensation = target_tx["compensation"]
        resolved_tx_id = target_tx["tx_id"]
        repo = target_tx["repo_name"]
        plan = _build_compensation_plan(repo, compensation)

        if dry_run:
            return {
                "status": "dry_run",
                "tx_id": resolved_tx_id,
                "original_tool": tool_name,
                "repo_name": repo,
                "compensation": compensation,
                "compensation_plan": plan,
                "message": f"Dry-Run Inspection for {resolved_tx_id}: {plan['summary']} (No state mutated).",
            }

        if not confirmed:
            return {
                "status": "confirmation_required",
                "action": "undo_last_action",
                "tx_id": resolved_tx_id,
                "target_tool": tool_name,
                "compensation": compensation,
                "compensation_plan": plan,
                "message": (
                    f"Confirmation Required: Undo action '{tool_name}' (Transaction ID: {resolved_tx_id})? "
                    f"Plan: {plan['summary']} Confirm with confirmed=True."
                ),
            }

        try:
            comp_type = compensation.get("type")

            if comp_type == "close_issue":
                res = gh.close_issue(repo, compensation["issue_number"], comment="Reverted via undo_last_action.")
            elif comp_type == "reopen_issue":
                res = gh.reopen_issue(repo, compensation["issue_number"])
            elif comp_type == "remove_label":
                res = gh.remove_label(repo, compensation["issue_number"], compensation["label"])
            elif comp_type == "bulk_remove_label":
                removed = []
                for num in compensation.get("issue_numbers", []):
                    gh.remove_label(repo, num, compensation["label"])
                    removed.append(num)
                res = {"status": "bulk_label_removed", "issue_numbers": removed, "label": compensation["label"]}
            else:
                return {
                    "status": "error",
                    "type": "unsupported_compensation",
                    "message": f"Compensation type '{comp_type}' is not supported.",
                }

            TransactionJournal.mark_reverted(resolved_tx_id)
            return {
                "status": "reverted",
                "tx_id": resolved_tx_id,
                "original_tool": tool_name,
                "compensation_plan": plan,
                "compensation_result": res,
            }

        except Exception as e:
            return {
                "status": "error",
                "type": "rollback_failed",
                "message": f"Failed to revert transaction {resolved_tx_id}: {str(e)}",
            }

