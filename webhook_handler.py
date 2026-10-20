"""
Event-Driven GitHub Webhook Receiver — GitHub MCP Toolkit
==========================================================
Implements an enterprise event-driven webhook pipeline for autonomous GitHub
issue governance:
  1. Constant-time HMAC-SHA256 signature verification (`X-Hub-Signature-256`)
     using `hmac.compare_digest` to prevent cryptographic timing side-channels.
  2. Thread-safe webhook replay protection (`X-GitHub-Delivery` nonce store
     with 10-minute TTL and bounded ring-buffer eviction).
  3. Autonomous `issues.opened` / `issues.reopened` reactive pipeline:
     - Scans incoming issue for accidentally leaked secrets/PII (DLP alert)
     - Scores untrusted issue text across 5 indirect prompt-injection signatures
     - Computes Hybrid RRF + TF-IDF duplicate/related issue matches
     - Produces priority/category classification + SLA remediation runbook
"""

import os
import hmac
import time
import json
import hashlib
import threading
from typing import Dict, Any, Optional, List, Tuple

from policy_engine import PolicyEngine
from vector_engine import VectorEngine
from tracer import get_or_create_trace, clear_active_trace
from tools.triage_issue import (
    detect_prompt_injection_signals,
    _classify_issue,
    _build_remediation_playbook,
)


class GitHubWebhookProcessor:
    """
    Cryptographically verified, replay-protected GitHub Webhook processor.
    """

    DELIVERY_TTL_SECONDS = 600  # 10-minute replay window
    MAX_DELIVERY_CACHE = 1000
    _DELIVERY_LOCK = threading.RLock()
    _seen_deliveries: Dict[str, float] = {}

    @classmethod
    def reset_delivery_cache(cls) -> None:
        """Clear seen delivery IDs (used in tests)."""
        with cls._DELIVERY_LOCK:
            cls._seen_deliveries.clear()

    @staticmethod
    def compute_signature(secret: str, raw_body: bytes) -> str:
        """Compute GitHub-format `sha256=<hex>` HMAC signature."""
        digest = hmac.new(
            secret.encode("utf-8"),
            msg=raw_body,
            digestmod=hashlib.sha256,
        ).hexdigest()
        return f"sha256={digest}"

    @classmethod
    def verify_signature(
        cls,
        raw_body: bytes,
        signature_header: Optional[str],
        secret: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """
        Verify `X-Hub-Signature-256` header using constant-time `hmac.compare_digest`.
        """
        webhook_secret = secret if secret is not None else os.getenv("GITHUB_WEBHOOK_SECRET", "")
        if not webhook_secret:
            return False, "Webhook secret is not configured."
        if not signature_header or not signature_header.startswith("sha256="):
            return False, "Missing or malformed X-Hub-Signature-256 header."

        expected = cls.compute_signature(webhook_secret, raw_body)
        if not hmac.compare_digest(expected, signature_header.strip()):
            return False, "HMAC-SHA256 signature mismatch (tampered payload or invalid secret)."
        return True, "Signature verified."

    @classmethod
    def check_and_record_delivery(cls, delivery_id: Optional[str]) -> Tuple[bool, str]:
        """
        Enforce single-use webhook delivery nonce (`X-GitHub-Delivery`) within TTL window
        to block replay attacks.
        """
        if not delivery_id or not delivery_id.strip():
            return False, "Missing X-GitHub-Delivery header."

        did = delivery_id.strip()
        now = time.monotonic()

        with cls._DELIVERY_LOCK:
            expired = [k for k, exp in cls._seen_deliveries.items() if now > exp]
            for k in expired:
                cls._seen_deliveries.pop(k, None)

            if did in cls._seen_deliveries:
                return (
                    False,
                    f"Replay attack blocked: delivery_id '{did}' was already processed.",
                )

            if len(cls._seen_deliveries) >= cls.MAX_DELIVERY_CACHE:
                oldest_key = min(cls._seen_deliveries, key=cls._seen_deliveries.get)
                cls._seen_deliveries.pop(oldest_key, None)

            cls._seen_deliveries[did] = now + cls.DELIVERY_TTL_SECONDS
            return True, "Delivery nonce accepted."

    @classmethod
    def process_webhook_event(
        cls,
        raw_body: bytes,
        headers: Dict[str, str],
        existing_open_issues: Optional[List[Dict[str, Any]]] = None,
        secret: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Verify HMAC-SHA256 signature, enforce replay protection, and execute
        autonomous issue triage & security scanning for `issues` webhook events.
        """
        trace, is_owner = get_or_create_trace("github_webhook_event")
        try:
            norm_headers = {k.lower(): v for k, v in (headers or {}).items()}
            sig_header = norm_headers.get("x-hub-signature-256")
            delivery_id = norm_headers.get("x-github-delivery")
            event_type = norm_headers.get("x-github-event", "issues")

            # 1. Constant-time HMAC-SHA256 verification
            with trace.span("webhook_hmac_verification") as sp:
                sig_ok, sig_reason = cls.verify_signature(raw_body, sig_header, secret=secret)
                if not sig_ok:
                    sp.finish(status="blocked", reason=sig_reason)
                    if is_owner:
                        trace.commit(outcome="policy_denied")
                    return {
                        "status": "signature_rejected",
                        "reason": sig_reason,
                    }
                sp.finish(status="ok")

            # 2. Replay nonce protection
            with trace.span("webhook_replay_guard") as sp:
                nonce_ok, nonce_reason = cls.check_and_record_delivery(delivery_id)
                if not nonce_ok:
                    sp.finish(status="blocked", reason=nonce_reason)
                    if is_owner:
                        trace.commit(outcome="policy_denied")
                    return {
                        "status": "replay_blocked",
                        "delivery_id": delivery_id,
                        "reason": nonce_reason,
                    }
                sp.finish(status="ok", delivery_id=delivery_id)

            payload = json.loads(raw_body.decode("utf-8"))
            action = payload.get("action", "")

            if event_type != "issues" or action not in {"opened", "reopened", "edited"}:
                if is_owner:
                    trace.commit(outcome="ignored")
                return {
                    "status": "ignored",
                    "event": event_type,
                    "action": action,
                    "delivery_id": delivery_id,
                }

            issue = payload.get("issue", {})
            repo_name = payload.get("repository", {}).get("name", "unknown-repo")
            issue_number = int(issue.get("number", 0))
            title = str(issue.get("title", ""))
            body = str(issue.get("body", "") or "")

            # 3. Inbound Credential / PII Leak Detection + Prompt Injection Scoring
            with trace.span("webhook_security_scan") as sp:
                leaked_secret_type = PolicyEngine.scan_for_secrets(f"{title}\n{body}")
                injection_report = detect_prompt_injection_signals(title, body)
                sp.finish(
                    status="ok",
                    secret_detected=bool(leaked_secret_type),
                    injection_detected=injection_report["injection_detected"],
                )

            # 4. Hybrid RRF Duplicate / Related Issue Detection + Triage Playbook
            with trace.span("webhook_autonomous_triage") as sp:
                candidates = [
                    i for i in (existing_open_issues or [])
                    if int(i.get("number", -1)) != issue_number
                ]
                rrf_matches = VectorEngine.hybrid_rrf_search(title, candidates, top_k=1)
                duplicate_match = (
                    rrf_matches[0]
                    if rrf_matches and rrf_matches[0].get("similarity_score", 0.0) >= 0.70
                    else None
                )
                classification = _classify_issue(title, body)
                gh_adapter = type("_WebhookGHAdapter", (), {"list_open_issues": staticmethod(lambda _r: candidates)})()
                playbook = _build_remediation_playbook(
                    gh=gh_adapter,
                    repo_name=repo_name,
                    issue_number=issue_number,
                    title=title,
                    body=body,
                    classification=classification,
                )
                sp.finish(status="ok", priority=classification["priority"])

            result = {
                "status": "processed",
                "delivery_id": delivery_id,
                "event": f"{event_type}.{action}",
                "repo": repo_name,
                "issue_number": issue_number,
                "security_alerts": {
                    "credential_or_pii_leak_detected": bool(leaked_secret_type),
                    "leaked_type": leaked_secret_type,
                    "prompt_injection_detected": injection_report["injection_detected"],
                    "injection_risk_score": injection_report["risk_score"],
                    "injection_signals": injection_report["matched_signals"],
                },
                "duplicate_check": {
                    "is_potential_duplicate": duplicate_match is not None,
                    "matched_issue": duplicate_match,
                },
                "autonomous_triage": classification,
                "remediation_playbook": playbook,
            }
            if is_owner:
                trace.commit(outcome="success")
            return result
        except Exception as exc:
            if is_owner:
                trace.commit(outcome="error")
            return {"status": "error", "message": str(exc)}
        finally:
            if is_owner:
                clear_active_trace()
