from typing import Dict, Any, Union
from vector_engine import VectorEngine


def register(mcp, gh):
    @mcp.tool()
    def semantic_search_issues(query: str, repo_name: str = "", top_k: int = 5) -> Union[list, dict]:
        """
        Search open issues using Vector Space TF-IDF Cosine Similarity.
        
        TRIGGER CONDITIONS: Use this for conceptual or semantic searches where the exact 
        keywords might differ (e.g. "user authentication crash", "database timeout", "login failure").
        Returns issues ranked by vector similarity score (0.0 to 1.0).
        """
        if not query or not query.strip():
            return {
                "status": "error",
                "type": "invalid_argument",
                "message": "query parameter cannot be empty for semantic_search_issues."
            }

        try:
            issues = gh.list_open_issues(repo_name if repo_name else None)
            ranked = VectorEngine.rank_documents(query.strip(), issues, top_k=top_k)
            return {
                "status": "success",
                "query": query,
                "total_matched": len(ranked),
                "results": ranked
            }
        except Exception as e:
            return {
                "status": "error",
                "type": "vector_search_failed",
                "message": str(e)
            }
