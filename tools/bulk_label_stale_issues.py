import hashlib
import time
import threading
from typing import Dict, Any, List
from policy_engine import PolicyEngine
from transaction_journal import TransactionJournal

_preview_store: Dict[str, Dict[str, Any]] = {}
_PREVIEW_LOCK = threading.Lock()
PREVIEW_TTL_SECONDS = 300  # 5 minutes
MAX_PREVIEW_TOKENS = 256


def _purge_expired_tokens_unlocked(now: float) -> None:
    """Remove expired preview tokens and cap store size (caller must hold _PREVIEW_LOCK)."""
    expired_keys = [k for k, v in _preview_store.items() if now > v.get("expires_at", 0)]
    for k in expired_keys:
        del _preview_store[k]
    while len(_preview_store) >= MAX_PREVIEW_TOKENS:
        oldest_key = min(_preview_store.keys(), key=lambda k: _preview_store[k].get("expires_at", 0))
        del _preview_store[oldest_key]


def _make_token(repo_name: str, issue_numbers: List[int], label: str) -> str:
    raw = f"{repo_name}:{sorted(issue_numbers)}:{label}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def register(mcp, gh):
    @mcp.tool()
    def bulk_label_stale_issues(
        repo_name: str,
        days_inactive: int = 30,
        label: str = "stale",
        confirmed: bool = False,
        preview_token: str = "",
    ) -> Dict[str, Any]:
        """
        Find open issues in a repository inactive for `days_inactive` days and apply a label to all of them.

        THIS IS A BULK WRITE ACTION WITH BLAST RADIUS.

        GUARDRAILS:
        - ABAC Policy Engine evaluation (max_bulk_limit, restricted_labels, write-freeze).
        - Two-Phase Preview-Token Flow (SHA-256 token with 5-minute TTL and single-use deletion).
        - Saga Transaction Journal recording for undo_last_action rollback.
        """
        if not repo_name or not repo_name.strip():
            return {
                "status": "error",
                "type": "invalid_argument",
                "message": "repo_name is required.",
            }

        try:
            rate_rem = PolicyEngine.get_client_rate_remaining(gh)
            # Pre-flight policy check for write-freeze, quota buffer, and restricted label
            allowed, reason = PolicyEngine.evaluate(
                "bulk_label_stale_issues",
                {"repo_name": repo_name.strip(), "label": label.strip()},
                rate_limit_remaining=rate_rem,
            )
            if not allowed:
                return {"status": "policy_denied", "reason": reason}

            # Phase 1: Confirmation Request & Preview Token Generation
            if not confirmed:
                stale = gh.find_stale_issues(repo_name.strip(), days_inactive)
                if not stale:
                    return {
                        "status": "no_action",
                        "message": f"No issues found in '{repo_name}' inactive for {days_inactive}+ days.",
                    }

                # Check bulk count cap against PolicyEngine
                allowed_bulk, reason_bulk = PolicyEngine.evaluate(
                    "bulk_label_stale_issues",
                    {"repo_name": repo_name.strip(), "label": label.strip(), "affected_count": len(stale)},
                    rate_limit_remaining=rate_rem,
                )
                if not allowed_bulk:
                    return {"status": "policy_denied", "reason": reason_bulk}

                issue_numbers = [i["number"] for i in stale]
                token = _make_token(repo_name.strip(), issue_numbers, label.strip())
                now = time.time()
                with _PREVIEW_LOCK:
                    _purge_expired_tokens_unlocked(now)
                    _preview_store[token] = {
                        "issue_numbers": issue_numbers,
                        "repo_name": repo_name.strip(),
                        "label": label.strip(),
                        "expires_at": now + PREVIEW_TTL_SECONDS,
                    }

                blast = PolicyEngine.estimate_blast_radius(
                    "bulk_label_stale_issues",
                    affected_entities=len(stale),
                    rate_limit_remaining=rate_rem,
                )
                return {
                    "status": "confirmation_required",
                    "preview_token": token,
                    "blast_radius": blast,
                    "affected_count": len(stale),
                    "affected_issues": stale,
                    "label_to_apply": label.strip(),
                    "ttl_seconds": PREVIEW_TTL_SECONDS,
                    "message": (
                        f"PREVIEW: {len(stale)} issue(s) in '{repo_name}' will be labeled '{label}'. "
                        f"Show this exact list to the user. To proceed, call again with "
                        f"confirmed=True and preview_token='{token}'."
                    ),
                }

            # Phase 2: Execution Validation
            if not preview_token:
                return {
                    "status": "error",
                    "type": "missing_preview_token",
                    "message": "Bulk write actions require a preview_token from Phase 1. Run with confirmed=False first.",
                }

            with _PREVIEW_LOCK:
                entry = _preview_store.get(preview_token)
                if not entry:
                    return {
                        "status": "error",
                        "type": "invalid_or_expired_token",
                        "message": "Preview token is invalid or expired. Re-run with confirmed=False to generate a fresh preview.",
                    }

                if time.time() > entry["expires_at"]:
                    del _preview_store[preview_token]
                    return {
                        "status": "error",
                        "type": "token_expired",
                        "message": "Preview token expired (5 min TTL). Re-run with confirmed=False to generate a fresh preview.",
                    }

                if entry["repo_name"] != repo_name.strip() or entry["label"] != label.strip():
                    return {
                        "status": "error",
                        "type": "token_mismatch",
                        "message": "Token does not match the target repo_name or label specified.",
                    }

                del _preview_store[preview_token]  # Single-use enforcement

            results = gh.bulk_add_label(repo_name.strip(), entry["issue_numbers"], label.strip())
            labeled_nums = [r["number"] for r in results if r.get("status") == "labeled"]
            tx_id = None
            if labeled_nums:
                tx_id = TransactionJournal.record_transaction(
                    tool="bulk_label_stale_issues",
                    repo_name=repo_name.strip(),
                    parameters={"issue_numbers": labeled_nums, "label": label.strip()},
                    compensation={"type": "bulk_remove_label", "issue_numbers": labeled_nums, "label": label.strip()},
                )

            return {
                "status": "completed",
                "repo_name": repo_name,
                "label": label,
                "total_affected": len(results),
                "transaction_id": tx_id,
                "results": results,
            }

        except Exception as e:
            return {
                "status": "error",
                "type": "github_api_error",
                "message": str(e),
            }
