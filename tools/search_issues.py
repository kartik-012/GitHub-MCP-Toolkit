from typing import Dict, Any, Union


def register(mcp, gh):
    @mcp.tool()
    def search_issues(keyword: str, repo_name: str = "") -> Union[list, dict]:
        """
        Search open issues by keyword in title or description text.
        
        TRIGGER CONDITIONS: Use this ONLY when the user's query includes a specific 
        search word, term, phrase, or topic filter (e.g., "find issues about login", 
        "search for bug in api").
        
        DO NOT USE THIS FOR: General "list open issues" or "what's open" queries without 
        keywords (use get_open_issues for general listing).
        """
        try:
            if not keyword or not keyword.strip():
                return {
                    "status": "error",
                    "type": "invalid_argument",
                    "message": "Keyword parameter cannot be empty for search_issues."
                }
            return gh.search_issues(keyword.strip(), repo_name if repo_name else None)
        except Exception as e:
            return {
                "status": "error",
                "type": "github_api_error",
                "message": str(e)
            }
