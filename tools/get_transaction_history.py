from typing import Dict, Any
from transaction_journal import TransactionJournal


def register(mcp, gh):
    @mcp.tool()
    def get_transaction_history(limit: int = 10) -> Dict[str, Any]:
        """
        List recent write transactions and their saga rollback status (committed / reverted).
        """
        try:
            history = TransactionJournal.list_history(limit=limit)
            return {
                "status": "success",
                "total_records": len(history),
                "history": history
            }
        except Exception as e:
            return {
                "status": "error",
                "type": "journal_read_failed",
                "message": str(e)
            }
