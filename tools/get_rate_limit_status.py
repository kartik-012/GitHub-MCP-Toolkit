from typing import Dict, Any
from schemas import validate, RateLimitResponse


def register(mcp, gh):
    @mcp.tool()
    def get_rate_limit_status() -> Dict[str, Any]:
        """
        Check current GitHub REST API rate limit quota, remaining requests, and reset time.
        Use this tool to monitor production API quota usage.
        """
        try:
            result = gh.get_rate_limit_status()
            return validate(RateLimitResponse, result)
        except Exception as e:
            return {
                "status": "error",
                "type": "github_api_error",
                "message": str(e)
            }
