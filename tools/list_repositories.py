from typing import Dict, Any


def register(mcp, gh):
    @mcp.tool()
    def list_repositories() -> Dict[str, Any]:
        """
        List all GitHub repositories accessible by the authenticated user.
        Use this tool when the user asks to see their repositories, projects,
        or wants to know which repos they have access to.
        """
        try:
            repos = gh.list_repositories()
            return {
                "status": "success",
                "total": len(repos),
                "repositories": repos,
            }
        except Exception as e:
            return {
                "status": "error",
                "type": "github_api_error",
                "message": str(e),
            }
