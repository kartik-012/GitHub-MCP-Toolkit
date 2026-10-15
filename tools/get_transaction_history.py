from typing import Dict, Any
from transaction_journal import TransactionJournal


def register(mcp, gh):
    @mcp.tool()
    def get_transaction_history(limit: int = 10) -> Dict[str, Any]:
        """
        List recent write transactions, their saga rollback status (committed / reverted),
        and cryptographic SHA-256 hash-chain integrity verification status.
        """
        try:
            history = TransactionJournal.list_history(limit=limit)
            integrity = TransactionJournal.verify_chain_integrity()
            return {
                "status": "success",
                "total_records": len(history),
                "integrity": integrity,
                "history": history,
            }
        except Exception as e:
            return {
                "status": "error",
                "type": "journal_read_failed",
                "message": str(e),
            }

