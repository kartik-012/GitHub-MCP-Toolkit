import os
import re
import json
import math
from collections import Counter
from typing import Dict, Any, Tuple, Optional, List

POLICY_FILE = os.path.join(os.path.dirname(__file__), "policy.json")

# Pre-compiled patterns for Outbound DLP / Secret & PII Leak Prevention
SECRET_PATTERNS = [
    (re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"), "GitHub Personal Access Token"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "GitHub Fine-Grained PAT"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS Access Key ID"),
    (re.compile(r"(?:sk_live_|rk_live_|sk-)[A-Za-z0-9_-]{20,}"), "API Secret Key (sk-...)"),
    (re.compile(r"xox[baprs]-[0-9A-Za-z-]{15,}"), "Slack Authentication Token"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "JSON Web Token (JWT)"),
    (
        re.compile(r"(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://[^:\s/]+:[^@\s/]{4,}@[^\s/]+", re.IGNORECASE),
        "Database Connection URI with Credentials",
    ),
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"), "PEM Private Key"),
    (re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b"), "PII (US Social Security Number)"),
    (re.compile(r"hf_[A-Za-z0-9]{34,}"), "Hugging Face Access Token"),
]

# Key-value assignment pattern for high-entropy custom secrets (e.g., DB_PASSWORD=..., JWT_SECRET=...)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?:password|passwd|secret|api_key|apikey|client_secret|auth_token|access_token|private_key)\s*[:=]\s*['\"]?([A-Za-z0-9+/=_-]{20,})['\"]?",
    re.IGNORECASE,
)

# Candidate 13-19 digit payment card pattern (validated via Luhn checksum to avoid false positives)
_CREDIT_CARD_RE = re.compile(r"\b(?:\d[ -]*?){13,19}\b")


class PolicyEngine:
    """
    Attribute-Based Access Control (ABAC) & Enterprise Outbound DLP Policy Engine.
    Evaluates enterprise security policies before any write action touches the GitHub API:
      1. Global Write Freeze (`allow_writes`)
      2. Outbound DLP: Vendor Secret Regexes + Shannon Entropy Scanner + Luhn PII Detection
      3. GitHub API Rate-Limit Quota Safety Buffer (`min_rate_limit_remaining`)
      4. Restricted System Labels (`restricted_labels`)
      5. Bulk Blast-Radius Cap (`max_bulk_limit`)
    """

    SHANNON_ENTROPY_THRESHOLD = 3.8  # bits per character

    @classmethod
    def load_policy(cls) -> Dict[str, Any]:
        """Load enterprise policy rules from policy.json with defensive safe defaults fallback."""
        if not os.path.exists(POLICY_FILE):
            return {
                "max_bulk_limit": 15,
                "min_rate_limit_remaining": 20,
                "restricted_labels": ["security-critical", "production-outage"],
                "allow_writes": True,
            }
        try:
            with open(POLICY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    @staticmethod
    def shannon_entropy(value: str) -> float:
        """
        Compute Shannon information entropy H(X) = -sum(p_i * log2(p_i)) in bits/char.
        Used to detect vendor-agnostic high-entropy cryptographic secrets.
        """
        if not value:
            return 0.0
        length = len(value)
        counts = Counter(value)
        return round(-sum((c / length) * math.log2(c / length) for c in counts.values()), 4)

    @classmethod
    def calculate_entropy_level(cls, value: str) -> str:
        """
        Categorize the Shannon information entropy of an arbitrary value:
        'high' (>= threshold 3.8), 'moderate' (>= 2.5), or 'low' (< 2.5).
        """
        entropy = cls.shannon_entropy(value)
        if entropy >= cls.SHANNON_ENTROPY_THRESHOLD:
            return "high"
        if entropy >= 2.5:
            return "moderate"
        return "low"

    @staticmethod
    def _passes_luhn(digits_str: str) -> bool:
        """Validate a numeric string using the ISO/IEC 7812 Luhn checksum algorithm."""
        digits = [int(d) for d in digits_str if d.isdigit()]
        if len(digits) < 13 or len(digits) > 19 or len(set(digits)) == 1:
            return False
        checksum = 0
        reverse_digits = digits[::-1]
        for idx, d in enumerate(reverse_digits):
            if idx % 2 == 1:
                doubled = d * 2
                checksum += doubled - 9 if doubled > 9 else doubled
            else:
                checksum += d
        return checksum % 10 == 0

    @classmethod
    def scan_for_secrets(cls, text: str) -> Optional[str]:
        """
        Scan `text` for leaked credentials, high-entropy secret assignments, or regulated PII.
        Returns the detected violation category name, or None if clean.
        """
        if not text:
            return None

        # 1. Deterministic vendor credential & SSN signatures
        for pattern, label in SECRET_PATTERNS:
            if pattern.search(text):
                return label

        # 2. Vendor-agnostic Shannon Entropy check on secret/password key-value assignments
        for match in _SECRET_ASSIGNMENT_RE.finditer(text):
            candidate = match.group(1)
            if cls.shannon_entropy(candidate) >= cls.SHANNON_ENTROPY_THRESHOLD:
                return "High-Entropy Secret Assignment"

        # 3. Luhn-validated Payment Card PII check
        for match in _CREDIT_CARD_RE.finditer(text):
            raw_candidate = match.group(0)
            if cls._passes_luhn(raw_candidate):
                return "PII (Payment Card Number - Luhn Validated)"

        return None

    @staticmethod
    def mask_secret(secret_val: str, visible_prefix: int = 4, visible_suffix: int = 4) -> str:
        """
        Mask sensitive credentials for audit logs and telemetry diagnostics,
        leaving small prefix and suffix visible for operator recognition.
        e.g. 'ghp_1234567890abcdef1234' -> 'ghp_************1234'.
        """
        if not secret_val:
            return ""
        s = str(secret_val)
        if len(s) <= visible_prefix + visible_suffix:
            return "*" * len(s)
        return s[:visible_prefix] + ("*" * (len(s) - visible_prefix - visible_suffix)) + s[-visible_suffix:]

    @classmethod
    def get_client_rate_remaining(cls, gh: Any) -> Optional[int]:
        """Safely extract integer remaining rate limit from a GitHubClient instance."""
        if gh is None or not hasattr(gh, "get_cached_rate_remaining"):
            return None
        try:
            rem = gh.get_cached_rate_remaining()
            return rem if isinstance(rem, int) else None
        except Exception:
            return None

    # Scoped Session RBAC — Least-Privilege Persona Matrix
    READ_ONLY_TOOLS = {
        "get_open_issues",
        "list_repositories",
        "search_issues",
        "semantic_search_issues",
        "get_rate_limit_status",
        "get_transaction_history",
        "get_trace_history",
    }
    ROLE_PERMISSIONS: Dict[str, Optional[set]] = {
        "read_only": READ_ONLY_TOOLS,
        "triage_operator": READ_ONLY_TOOLS | {"triage_issue", "add_label"},
        "maintainer": None,  # Full tool access (governed by ABAC + 2-phase confirmation)
        "admin": None,
    }

    @classmethod
    def check_role_permission(cls, role: str, tool_name: str) -> Tuple[bool, str]:
        """Verify whether `role` is authorized to execute `tool_name` under Least-Privilege RBAC."""
        normalized_role = (role or "maintainer").strip().lower()
        if normalized_role not in cls.ROLE_PERMISSIONS:
            return (
                False,
                f"Policy Denial (RBAC Least-Privilege): Unknown agent role '{normalized_role}'.",
            )
        allowed_set = cls.ROLE_PERMISSIONS[normalized_role]
        if allowed_set is not None and tool_name not in allowed_set:
            return (
                False,
                f"Policy Denial (RBAC Least-Privilege): Agent role '{normalized_role}' is not "
                f"authorized to invoke '{tool_name}'. Allowed tools for '{normalized_role}': "
                f"{sorted(allowed_set)}.",
            )
        return True, "Allowed"

    @classmethod
    def evaluate(
        cls,
        tool_name: str,
        args: Dict[str, Any],
        rate_limit_remaining: Optional[int] = None,
    ) -> Tuple[bool, str]:
        """
        Evaluate tool execution against RBAC, ABAC policy, and DLP rules.
        Returns (allowed: bool, reason: str).
        """
        policy = cls.load_policy()

        # 0. Check Scoped Session RBAC (Agent Persona Least-Privilege)
        active_role = os.getenv("MCP_AGENT_ROLE") or policy.get("active_role", "maintainer")
        rbac_ok, rbac_reason = cls.check_role_permission(str(active_role), tool_name)
        if not rbac_ok:
            return False, rbac_reason

        # 1. Check Global Write Freeze
        write_tools = {"create_issue", "add_label", "close_issue", "bulk_label_stale_issues"}
        if not policy.get("allow_writes", True) and tool_name in write_tools:
            return False, "Policy Denial: Global write operations are currently frozen by system policy."

        # 2. Outbound DLP / Secret Leak Scan on free-text write fields
        for field in ("title", "body", "comment"):
            val = args.get(field, "")
            if isinstance(val, str) and val:
                secret_type = cls.scan_for_secrets(val)
                if secret_type:
                    return (
                        False,
                        f"Policy Denial (DLP): Detected potential {secret_type} in '{field}'. "
                        f"Outbound write blocked to prevent credential leakage.",
                    )

        # 3. Check Rate Limit Safety Buffer
        min_quota = policy.get("min_rate_limit_remaining", 20)
        if rate_limit_remaining is not None and rate_limit_remaining < min_quota:
            return (
                False,
                f"Policy Denial: GitHub API quota below safety buffer "
                f"({rate_limit_remaining} < {min_quota}). Write actions blocked.",
            )

        # 4. Check Restricted Label Policy
        label = args.get("label")
        if label and label.lower() in [l.lower() for l in policy.get("restricted_labels", [])]:
            return False, f"Policy Denial: Label '{label}' is classified as a restricted system label."

        # 5. Check Bulk Action Limit Policy
        if tool_name == "bulk_label_stale_issues":
            affected_count = args.get("affected_count", 0)
            max_limit = policy.get("max_bulk_limit", 15)
            if affected_count > max_limit:
                return (
                    False,
                    f"Policy Denial: Bulk action affects {affected_count} issues, "
                    f"exceeding maximum policy limit of {max_limit}.",
                )

        # 6. Sliding-Window Write Velocity Limiter (Runaway Agent Loop Breaker)
        if tool_name in write_tools:
            max_wpm = int(policy.get("max_writes_per_minute", 0) or 0)
            if max_wpm > 0:
                vel_ok, vel_reason = cls.check_and_record_velocity(tool_name, max_wpm)
                if not vel_ok:
                    return False, vel_reason

        # 7. Per-Session / Multi-Tenant Token Budget Ceiling
        max_sess_tok = int(policy.get("max_session_tokens", 0) or 0)
        if max_sess_tok > 0:
            sess_ok, sess_reason = cls.check_session_budget(args.get("session_id"), max_sess_tok)
            if not sess_ok:
                return False, sess_reason

        return True, "Allowed"

    # ------------------------------------------------------------------
    # Sliding-Window Velocity Limiter (Runaway Agent Loop Breaker)
    # ------------------------------------------------------------------

    _VELOCITY_LOG: Dict[str, List[float]] = {}
    _VELOCITY_LOCK = __import__("threading").Lock()
    VELOCITY_WINDOW_SECONDS = 60.0

    @classmethod
    def reset_velocity_counters(cls) -> None:
        """Clear all sliding-window write velocity timestamps."""
        with cls._VELOCITY_LOCK:
            cls._VELOCITY_LOG.clear()

    @classmethod
    def check_and_record_velocity(cls, tool_name: str, max_per_minute: int) -> Tuple[bool, str]:
        """
        Enforce a thread-safe 60-second sliding-window velocity cap across write tools
        to halt runaway LLM tool-calling loops.
        """
        import time

        now = time.time()
        cutoff = now - cls.VELOCITY_WINDOW_SECONDS
        with cls._VELOCITY_LOCK:
            history = [t for t in cls._VELOCITY_LOG.get("__global_writes__", []) if t > cutoff]
            if len(history) >= max_per_minute:
                cls._VELOCITY_LOG["__global_writes__"] = history
                return (
                    False,
                    f"Policy Denial (Runaway Agent Loop Breaker): Write velocity limit exceeded "
                    f"({len(history)}/{max_per_minute} writes in {int(cls.VELOCITY_WINDOW_SECONDS)}s window) "
                    f"while invoking '{tool_name}'.",
                )
            history.append(now)
            cls._VELOCITY_LOG["__global_writes__"] = history
            return True, "Allowed"

    # ------------------------------------------------------------------
    # Cryptographic 2-Phase Preview Token Handshake for Write Tools
    # ------------------------------------------------------------------

    _WRITE_TOKEN_STORE: Dict[str, Dict[str, Any]] = {}
    _WRITE_TOKEN_LOCK = __import__("threading").Lock()
    WRITE_TOKEN_TTL_SECONDS = 300
    MAX_WRITE_TOKENS = 256

    @classmethod
    def _purge_expired_write_tokens_unlocked(cls, now: float) -> None:
        expired = [k for k, v in cls._WRITE_TOKEN_STORE.items() if now > v.get("expires_at", 0)]
        for k in expired:
            del cls._WRITE_TOKEN_STORE[k]
        while len(cls._WRITE_TOKEN_STORE) >= cls.MAX_WRITE_TOKENS:
            oldest = min(cls._WRITE_TOKEN_STORE.keys(), key=lambda k: cls._WRITE_TOKEN_STORE[k].get("expires_at", 0))
            del cls._WRITE_TOKEN_STORE[oldest]

    @classmethod
    def issue_write_preview_token(cls, tool_name: str, fingerprint_payload: Dict[str, Any]) -> str:
        """
        Generate a single-use SHA-256 preview token bound to `tool_name` and `fingerprint_payload`
        with a 5-minute TTL.
        """
        import hashlib
        import time

        canonical = f"{tool_name}:{json.dumps(fingerprint_payload, sort_keys=True)}"
        token = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        now = time.time()
        with cls._WRITE_TOKEN_LOCK:
            cls._purge_expired_write_tokens_unlocked(now)
            cls._WRITE_TOKEN_STORE[token] = {
                "tool_name": tool_name,
                "fingerprint": fingerprint_payload,
                "expires_at": now + cls.WRITE_TOKEN_TTL_SECONDS,
            }
        return token

    @classmethod
    def verify_write_preview_token(
        cls,
        tool_name: str,
        fingerprint_payload: Dict[str, Any],
        preview_token: str = "",
    ) -> Tuple[bool, str]:
        """
        Verify a single-item write preview token.
        - When `preview_token` is supplied, strictly validates existence, TTL, and exact
          parameter binding (`tool_name` + `fingerprint_payload`), then consumes the token.
        - When `preview_token` is omitted, enforces token presence if `require_write_preview_token`
          is True in `policy.json` (default False for backward compatibility).
        """
        import time

        policy = cls.load_policy()
        strict_mode = bool(policy.get("require_write_preview_token", False))
        tok = (preview_token or "").strip()

        if not tok:
            if strict_mode:
                return (
                    False,
                    f"Missing preview_token: Strict 2-Phase Write Token policy requires calling "
                    f"'{tool_name}' with confirmed=False first to obtain a preview_token.",
                )
            return True, "Allowed"

        now = time.time()
        with cls._WRITE_TOKEN_LOCK:
            cls._purge_expired_write_tokens_unlocked(now)
            entry = cls._WRITE_TOKEN_STORE.get(tok)
            if not entry:
                return False, "Invalid or expired preview_token. Run with confirmed=False to generate a fresh preview."
            if entry["tool_name"] != tool_name or entry["fingerprint"] != fingerprint_payload:
                return (
                    False,
                    f"Preview token parameter mismatch: token was not issued for these exact '{tool_name}' parameters.",
                )
            del cls._WRITE_TOKEN_STORE[tok]  # Single-use consumption
            return True, "Verified"

    # ------------------------------------------------------------------
    # Pre-Flight Blast-Radius, Quota Impact & Reversibility Simulator
    # ------------------------------------------------------------------

    _ROLLBACK_MAP = {
        "create_issue": ("close_issue", 2, 0.25),
        "add_label": ("remove_label", 1, 0.15),
        "close_issue": ("reopen_issue", 1, 0.35),
        "bulk_label_stale_issues": ("bulk_remove_label", 1, 0.50),
    }

    @classmethod
    def estimate_blast_radius(
        cls,
        tool_name: str,
        affected_entities: int = 1,
        rate_limit_remaining: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Compute a deterministic Pre-Flight Blast-Radius, API Quota Impact,
        and Saga Reversibility assessment before executing a write tool.
        """
        rollback_op, base_calls, base_weight = cls._ROLLBACK_MAP.get(
            tool_name, ("none", 1, 0.10)
        )
        n_entities = max(1, int(affected_entities))
        est_calls = (n_entities + 1) if tool_name == "bulk_label_stale_issues" else base_calls * n_entities

        # Scale blast radius by number of affected entities
        raw_score = base_weight + (0.05 * max(0, n_entities - 1))
        score = round(min(1.0, raw_score), 2)

        if score >= 0.75:
            tier = "critical"
        elif score >= 0.45:
            tier = "high"
        elif score >= 0.25:
            tier = "medium"
        else:
            tier = "low"

        quota_pct = (
            round((est_calls / max(1, rate_limit_remaining)) * 100.0, 3)
            if isinstance(rate_limit_remaining, int) and rate_limit_remaining > 0
            else 0.0
        )

        return {
            "tool": tool_name,
            "affected_entities": n_entities,
            "estimated_api_calls": est_calls,
            "quota_impact_pct": quota_pct,
            "blast_radius_score": score,
            "severity_tier": tier,
            "reversible_via_saga": rollback_op != "none",
            "rollback_operation": rollback_op,
        }

    # ------------------------------------------------------------------
    # Cryptographic SOC2 / ISO-27001 Compliance & Governance Snapshot
    # ------------------------------------------------------------------

    @classmethod
    def export_compliance_snapshot(cls) -> Dict[str, Any]:
        """
        Generate a cryptographically attested SOC2 / ISO-27001 Governance & Compliance
        Snapshot combining active RBAC/ABAC policy, SHA-256 transaction journal integrity,
        live SLO telemetry percentiles, and active security controls.
        """
        import hashlib
        import time
        from transaction_journal import TransactionJournal
        from tracer import get_telemetry_summary

        policy = cls.load_policy()
        integrity = TransactionJournal.verify_chain_integrity()
        slo = get_telemetry_summary()

        controls = {
            "outbound_dlp_pattern_count": len(SECRET_PATTERNS),
            "shannon_entropy_secret_detection": True,
            "luhn_payment_card_validation": True,
            "two_phase_write_preview_tokens": True,
            "sliding_window_velocity_limiter": True,
            "scoped_rbac_least_privilege": True,
            "indirect_prompt_injection_scorer": True,
            "tamper_evident_sha256_journal": True,
        }

        compliant = bool(
            integrity.get("chain_valid", False)
            and slo.get("slo_status") == "healthy"
        )

        body_payload = {
            "framework": "SOC2-Type-II / ISO-27001 Agentic Governance",
            "compliant": compliant,
            "active_policy": policy,
            "journal_integrity": integrity,
            "slo_telemetry": slo,
            "security_controls": controls,
        }
        canonical = json.dumps(body_payload, sort_keys=True, separators=(",", ":"))
        attestation_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

        return {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "snapshot_sha256": attestation_hash,
            **body_payload,
        }

    # ------------------------------------------------------------------
    # Multi-Tenant / Per-Session FinOps & Token Attribution Ledger
    # ------------------------------------------------------------------

    _SESSION_LEDGER: Dict[str, Dict[str, Any]] = {}
    _SESSION_LOCK = __import__("threading").RLock()
    COST_PER_1K_TOKENS_USD = 0.003  # $3.00 / 1M context tokens

    @classmethod
    def reset_session_ledger(cls) -> None:
        """Clear all recorded per-session FinOps attribution metrics."""
        with cls._SESSION_LOCK:
            cls._SESSION_LEDGER.clear()

    @classmethod
    def record_session_usage(
        cls,
        session_id: Optional[str],
        tool_name: str,
        tokens_returned: int,
        tokens_saved: int = 0,
        api_calls: int = 1,
    ) -> Dict[str, Any]:
        """
        Attribute LLM context token consumption, adaptive compaction savings,
        and GitHub API call usage to a specific tenant/session (`MCP_SESSION_ID`).
        """
        sid = (session_id or os.getenv("MCP_SESSION_ID") or "default_session").strip()
        tok_ret = max(0, int(tokens_returned))
        tok_sav = max(0, int(tokens_saved))
        calls = max(0, int(api_calls))

        with cls._SESSION_LOCK:
            bucket = cls._SESSION_LEDGER.setdefault(
                sid,
                {
                    "session_id": sid,
                    "tool_invocations": 0,
                    "api_calls_spent": 0,
                    "tokens_returned": 0,
                    "tokens_saved_by_compaction": 0,
                    "estimated_cost_usd": 0.0,
                    "estimated_savings_usd": 0.0,
                    "per_tool_calls": {},
                },
            )
            bucket["tool_invocations"] += 1
            bucket["api_calls_spent"] += calls
            bucket["tokens_returned"] += tok_ret
            bucket["tokens_saved_by_compaction"] += tok_sav
            bucket["estimated_cost_usd"] = round(
                (bucket["tokens_returned"] / 1000.0) * cls.COST_PER_1K_TOKENS_USD, 6
            )
            bucket["estimated_savings_usd"] = round(
                (bucket["tokens_saved_by_compaction"] / 1000.0) * cls.COST_PER_1K_TOKENS_USD, 6
            )
            bucket["per_tool_calls"][tool_name] = bucket["per_tool_calls"].get(tool_name, 0) + 1
            return dict(bucket)

    @classmethod
    def check_session_budget(
        cls,
        session_id: Optional[str] = None,
        max_session_tokens: Optional[int] = None,
    ) -> Tuple[bool, str]:
        """
        Verify whether `session_id` is within its allocated context-token budget ceiling.
        """
        sid = (session_id or os.getenv("MCP_SESSION_ID") or "default_session").strip()
        limit = (
            max_session_tokens
            if max_session_tokens is not None
            else int(cls.load_policy().get("max_session_tokens", 0) or 0)
        )
        if limit <= 0:
            return True, "Unlimited session token budget."

        with cls._SESSION_LOCK:
            used = cls._SESSION_LEDGER.get(sid, {}).get("tokens_returned", 0)
        if used >= limit:
            return (
                False,
                f"Policy Denial (Session FinOps Budget): Session '{sid}' has exhausted its "
                f"token budget ceiling ({used} >= {limit} tokens).",
            )
        return True, "Within session token budget."

    @classmethod
    def get_finops_report(cls, session_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Return multi-tenant FinOps & Token Attribution analytics across all active sessions
        or filtered to a single `session_id`.
        """
        with cls._SESSION_LOCK:
            if session_id:
                sid = session_id.strip()
                sessions = {sid: dict(cls._SESSION_LEDGER[sid])} if sid in cls._SESSION_LEDGER else {}
            else:
                sessions = {k: dict(v) for k, v in cls._SESSION_LEDGER.items()}

        total_returned = sum(s["tokens_returned"] for s in sessions.values())
        total_saved = sum(s["tokens_saved_by_compaction"] for s in sessions.values())
        total_raw = total_returned + total_saved
        compaction_ratio_pct = round((total_saved / total_raw) * 100.0, 2) if total_raw > 0 else 0.0

        return {
            "active_sessions": len(sessions),
            "total_tokens_returned": total_returned,
            "total_tokens_saved": total_saved,
            "compaction_efficiency_pct": compaction_ratio_pct,
            "total_estimated_cost_usd": round((total_returned / 1000.0) * cls.COST_PER_1K_TOKENS_USD, 6),
            "total_estimated_savings_usd": round((total_saved / 1000.0) * cls.COST_PER_1K_TOKENS_USD, 6),
            "sessions": sessions,
        }



