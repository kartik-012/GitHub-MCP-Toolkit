import os
import json
from typing import Dict, Any, Tuple, Optional

POLICY_FILE = os.path.join(os.path.dirname(__file__), "policy.json")


class PolicyEngine:
    """
    Attribute-Based Access Control (ABAC) Policy Engine.
    Evaluates enterprise security policies (max bulk count, restricted labels,
    rate limit quota buffers) before any write action touches the GitHub API.
    """

    @classmethod
    def load_policy(cls) -> Dict[str, Any]:
        if not os.path.exists(POLICY_FILE):
            return {
                "max_bulk_limit": 15,
                "min_rate_limit_remaining": 20,
                "restricted_labels": ["security-critical", "production-outage"],
                "allow_writes": True
            }
        try:
            with open(POLICY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    @classmethod
    def evaluate(cls, tool_name: str, args: Dict[str, Any], rate_limit_remaining: Optional[int] = None) -> Tuple[bool, str]:
        """
        Evaluate tool execution against policy rules.
        Returns (allowed: bool, reason: str).
        """
        policy = cls.load_policy()

        # Check Global Write Freeze
        if not policy.get("allow_writes", True) and tool_name in ["create_issue", "add_label", "close_issue", "bulk_label_stale_issues"]:
            return False, "Policy Denial: Global write operations are currently frozen by system policy."

        # Check Rate Limit Safety Buffer
        if rate_limit_remaining is not None and rate_limit_remaining < policy.get("min_rate_limit_remaining", 20):
            return False, f"Policy Denial: GitHub API quota below safety buffer ({rate_limit_remaining} < {policy.get('min_rate_limit_remaining')}). Write actions blocked."

        # Check Restricted Label Policy
        label = args.get("label")
        if label and label.lower() in [l.lower() for l in policy.get("restricted_labels", [])]:
            return False, f"Policy Denial: Label '{label}' is classified as a restricted system label."

        # Check Bulk Action Limit Policy
        if tool_name == "bulk_label_stale_issues":
            affected_count = args.get("affected_count", 0)
            max_limit = policy.get("max_bulk_limit", 15)
            if affected_count > max_limit:
                return False, f"Policy Denial: Bulk action affects {affected_count} issues, exceeding maximum policy limit of {max_limit}."

        return True, "Allowed"
