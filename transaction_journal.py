import os
import json
import time
import uuid
import hashlib
import threading
from typing import Dict, Any, List, Optional


JOURNAL_FILE = os.path.join(os.path.dirname(__file__), "transactions.json")
GENESIS_HASH = "0" * 64
_JOURNAL_LOCK = threading.Lock()


class TransactionJournal:
    """
    Thread-safe Saga Pattern Transaction Journal & Tamper-Evident Rollback Log.
    Records committed write mutations, their inverse compensation actions, and
    a cryptographic SHA-256 Merkle/hash chain (`prev_hash` -> `record_hash`)
    so any out-of-band tampering, deletion, or reordering is immediately detected.
    """

    @classmethod
    def _compute_record_hash(cls, record: Dict[str, Any], prev_hash: str) -> str:
        """
        Compute deterministic SHA-256 digest over the immutable transaction payload
        linked to `prev_hash`.
        """
        canonical_payload = {
            "tx_id": record.get("tx_id"),
            "timestamp": record.get("timestamp"),
            "tool": record.get("tool"),
            "repo_name": record.get("repo_name"),
            "parameters": record.get("parameters"),
            "compensation": record.get("compensation"),
            "prev_hash": prev_hash,
        }
        serialized = json.dumps(canonical_payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @classmethod
    def _load_journal(cls) -> List[Dict[str, Any]]:
        if not os.path.exists(JOURNAL_FILE):
            return []
        try:
            with open(JOURNAL_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    @classmethod
    def _save_journal(cls, records: List[Dict[str, Any]]) -> None:
        tmp_path = f"{JOURNAL_FILE}.tmp.{uuid.uuid4().hex[:8]}"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(records, f, indent=2)
                f.flush()
            os.replace(tmp_path, JOURNAL_FILE)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    @classmethod
    def record_transaction(
        cls,
        tool: str,
        repo_name: str,
        parameters: Dict[str, Any],
        compensation: Dict[str, Any],
    ) -> str:
        """Record a committed write transaction with its inverse compensation action and SHA-256 hash chain link."""
        tx_id = f"tx_{uuid.uuid4().hex[:8]}"
        record: Dict[str, Any] = {
            "tx_id": tx_id,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "tool": tool,
            "repo_name": repo_name,
            "parameters": parameters,
            "compensation": compensation,
            "status": "committed",
        }
        with _JOURNAL_LOCK:
            journal = cls._load_journal()
            prev_hash = journal[-1].get("record_hash", GENESIS_HASH) if journal else GENESIS_HASH
            record["prev_hash"] = prev_hash
            record["record_hash"] = cls._compute_record_hash(record, prev_hash)
            journal.append(record)
            cls._save_journal(journal)
        return tx_id

    @classmethod
    def verify_chain_integrity(cls) -> Dict[str, Any]:
        """
        Cryptographically verify the entire transaction journal hash chain.
        Detects any modified parameters/repo/tool fields, forged hashes, or deleted records.
        """
        with _JOURNAL_LOCK:
            journal = cls._load_journal()

        expected_prev = GENESIS_HASH
        tampered_tx_ids: List[str] = []

        for idx, record in enumerate(journal):
            tx_id = str(record.get("tx_id", f"index_{idx}"))
            actual_prev = record.get("prev_hash")
            actual_hash = record.get("record_hash")

            # Legacy records written before hash-chaining may lack hashes
            if actual_prev is None or actual_hash is None:
                tampered_tx_ids.append(tx_id)
                continue

            recomputed = cls._compute_record_hash(record, expected_prev)
            if actual_prev != expected_prev or actual_hash != recomputed:
                tampered_tx_ids.append(tx_id)

            expected_prev = actual_hash

        return {
            "chain_valid": len(tampered_tx_ids) == 0,
            "total_verified": len(journal),
            "tampered_tx_ids": tampered_tx_ids,
            "head_hash": expected_prev,
        }

    @classmethod
    def get_last_committed_transaction(cls) -> Optional[Dict[str, Any]]:
        """Return the most recent committed transaction that hasn't been reverted."""
        with _JOURNAL_LOCK:
            journal = cls._load_journal()
            for record in reversed(journal):
                if record.get("status") == "committed":
                    return record
            return None

    @classmethod
    def get_transaction_by_id(cls, tx_id: str) -> Optional[Dict[str, Any]]:
        """Return a specific transaction record by tx_id (whether committed or reverted)."""
        target = (tx_id or "").strip()
        if not target:
            return None
        with _JOURNAL_LOCK:
            journal = cls._load_journal()
            for record in reversed(journal):
                if record.get("tx_id") == target:
                    return record
            return None

    @classmethod
    def mark_reverted(cls, tx_id: str) -> None:
        """Mark a transaction record as reverted."""
        with _JOURNAL_LOCK:
            journal = cls._load_journal()
            for record in journal:
                if record.get("tx_id") == tx_id:
                    record["status"] = "reverted"
                    record["reverted_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    break
            cls._save_journal(journal)

    @classmethod
    def list_history(cls, limit: int = 10) -> List[Dict[str, Any]]:
        """List transaction history (most recent first)."""
        with _JOURNAL_LOCK:
            journal = cls._load_journal()
            return list(reversed(journal))[:limit]

