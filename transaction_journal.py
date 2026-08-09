import os
import json
import time
import uuid
from typing import Dict, Any, List, Optional


JOURNAL_FILE = os.path.join(os.path.dirname(__file__), "transactions.json")


class TransactionJournal:
    """
    Saga Pattern Transaction Journal & Rollback Log.
    Records committed write mutations and their inverse compensation actions,
    enabling state recovery via undo_last_action.
    """

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
    def _save_journal(cls, records: List[Dict[str, Any]]):
        with open(JOURNAL_FILE, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2)

    @classmethod
    def record_transaction(
        cls,
        tool: str,
        repo_name: str,
        parameters: Dict[str, Any],
        compensation: Dict[str, Any]
    ) -> str:
        """
        Record a committed write transaction with its inverse compensation action.
        """
        tx_id = f"tx_{uuid.uuid4().hex[:8]}"
        record = {
            "tx_id": tx_id,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "tool": tool,
            "repo_name": repo_name,
            "parameters": parameters,
            "compensation": compensation,
            "status": "committed"
        }
        journal = cls._load_journal()
        journal.append(record)
        cls._save_journal(journal)
        return tx_id

    @classmethod
    def get_last_committed_transaction(cls) -> Optional[Dict[str, Any]]:
        """Return the most recent committed transaction that hasn't been reverted."""
        journal = cls._load_journal()
        for record in reversed(journal):
            if record.get("status") == "committed":
                return record
        return None

    @classmethod
    def mark_reverted(cls, tx_id: str):
        """Mark a transaction record as reverted."""
        journal = cls._load_journal()
        for record in journal:
            if record.get("tx_id") == tx_id:
                record["status"] = "reverted"
                record["reverted_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                break
        cls._save_journal(journal)

    @classmethod
    def list_history(cls, limit: int = 10) -> List[Dict[str, Any]]:
        """List transaction history."""
        journal = cls._load_journal()
        return list(reversed(journal))[:limit]
