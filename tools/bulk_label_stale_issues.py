import hashlib
import time
from typing import Dict, Any, List

_preview_store: Dict[str, Dict[str, Any]] = {}
PREVIEW_TTL_SECONDS = 300  # 5 minutes


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
        preview_token: str = ""
    ) -> Dict[str, Any]:
        """
        Find open issues in a repository inactive for `days_inactive` days and apply a label to all of them.
        
        THIS IS A BULK WRITE ACTION WITH BLAST RADIUS.
        
        TWO-PHASE PREVIEW-TOKEN FLOW:
        1. Call first with confirmed=False (leave preview_token empty). This returns a preview list 
           of affected issues and a short-lived server preview_token.
        2. Show the complete preview list to the user.
        3. Call a second time with confirmed=True AND the EXACT preview_token returned in step 1.
        """
        if not repo_name or not repo_name.strip():
            return {
                "status": "error",
                "type": "invalid_argument",
                "message": "repo_name is required."
            }

        try:
            # Phase 1: Confirmation Request & Preview Token Generation
            if not confirmed:
                stale = gh.find_stale_issues(repo_name.strip(), days_inactive)
                if not stale:
                    return {
                        "status": "no_action",
                        "message": f"No issues found in '{repo_name}' inactive for {days_inactive}+ days."
                    }
                
                issue_numbers = [i["number"] for i in stale]
                token = _make_token(repo_name.strip(), issue_numbers, label.strip())
                _preview_store[token] = {
                    "issue_numbers": issue_numbers,
                    "repo_name": repo_name.strip(),
                    "label": label.strip(),
                    "expires_at": time.time() + PREVIEW_TTL_SECONDS
                }

                return {
                    "status": "confirmation_required",
                    "preview_token": token,
                    "affected_count": len(stale),
                    "affected_issues": stale,
                    "label_to_apply": label.strip(),
                    "ttl_seconds": PREVIEW_TTL_SECONDS,
                    "message": (
                        f"PREVIEW: {len(stale)} issue(s) in '{repo_name}' will be labeled '{label}'. "
                        f"Show this exact list to the user. To proceed, call again with "
                        f"confirmed=True and preview_token='{token}'."
                    )
                }

            # Phase 2: Execution Validation
            if not preview_token:
                return {
                    "status": "error",
                    "type": "missing_preview_token",
                    "message": "Bulk write actions require a preview_token from Phase 1. Run with confirmed=False first."
                }

            entry = _preview_store.get(preview_token)
            if not entry:
                return {
                    "status": "error",
                    "type": "invalid_or_expired_token",
                    "message": "Preview token is invalid or expired. Re-run with confirmed=False to generate a fresh preview."
                }

            if time.time() > entry["expires_at"]:
                del _preview_store[preview_token]
                return {
                    "status": "error",
                    "type": "token_expired",
                    "message": "Preview token expired (5 min TTL). Re-run with confirmed=False to generate a fresh preview."
                }

            if entry["repo_name"] != repo_name.strip() or entry["label"] != label.strip():
                return {
                    "status": "error",
                    "type": "token_mismatch",
                    "message": "Token does not match the target repo_name or label specified."
                }

            results = gh.bulk_add_label(repo_name.strip(), entry["issue_numbers"], label.strip())
            del _preview_store[preview_token]  # Single-use enforcement
            return {
                "status": "completed",
                "repo_name": repo_name,
                "label": label,
                "total_affected": len(results),
                "results": results
            }

        except Exception as e:
            return {
                "status": "error",
                "type": "github_api_error",
                "message": str(e)
            }
