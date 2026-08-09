from typing import Dict, Any, Union


def register(mcp, gh):
    @mcp.tool()
    def get_open_issues(repo_name: str = "") -> Union[list, dict]:
        """
        Get all currently open issues from the user's GitHub account or specified repo.
        
        TRIGGER CONDITIONS: Use this for general "what's open", "list all issues", 
        or "show open issues" questions across one or all repositories.
        
        DO NOT USE THIS FOR: Searches with specific keywords, titles, or filter terms.
        Use search_issues for keyword/filtered searches instead.
        """
        try:
            return gh.list_open_issues(repo_name if repo_name else None)
        except Exception as e:
            return {
                "status": "error",
                "type": "github_api_error",
                "message": str(e)
            }
