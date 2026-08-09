import os
import time
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv
from github import Github, GithubException, RateLimitExceededException
from circuit_breaker import CircuitBreaker, CircuitBreakerOpenError

# Ensure .env is loaded from the directory this script lives in
env_path = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(dotenv_path=env_path)

logger = logging.getLogger("github_mcp_toolkit")


class GitHubClient:
    """
    Production-grade GitHub API client wrapper for MCP server.
    Implements caching, PR filtering, rate limit handling with exponential
    backoff, pagination iteration, write idempotency checks, and a
    Circuit Breaker that trips after 3 consecutive failures to prevent
    cascading API errors from reaching the LLM.
    """

    def __init__(self, token: Optional[str] = None):
        self.token = token or os.getenv("GITHUB_TOKEN")
        if not self.token:
            # Fallback/mock mode notice if token not provided, raised on real API access
            self.gh = None
            self.user = None
        else:
            self.gh = Github(self.token)
            self._user_cache = None

        # Per-instance circuit breaker — prevents cascading API failures.
        # In a multi-endpoint system you would create one breaker per endpoint.
        self._breaker = CircuitBreaker(
            name="github_api",
            failure_threshold=3,
            cooldown_seconds=60.0,
        )

        # Short TTL in-memory cache for read calls
        self._cache: Dict[str, Dict[str, Any]] = {}
        self.cache_ttl = 60  # seconds

    def _get_authenticated_user(self):
        if not self.gh:
            raise ValueError("GITHUB_TOKEN is not set in environment or config.")
        if self._user_cache is None:
            self._user_cache = self._call_with_retry(lambda: self.gh.get_user())
        return self._user_cache

    def _call_with_retry(self, func, max_retries: int = 3, backoff_factor: float = 1.5):
        """
        Execute a PyGithub API function with:
          1. Circuit Breaker guard — rejects immediately when breaker is OPEN.
          2. Exponential back-off on rate-limit / 429 / 403 responses.

        The CircuitBreaker.call() method is the single source of truth for
        state checks and failure counting — we never manually inspect or
        mutate breaker internals here.
        """
        for attempt in range(max_retries):
            try:
                result = self._breaker.call(func)
                return result
            except CircuitBreakerOpenError:
                raise  # Circuit tripped — propagate immediately, no retry
            except RateLimitExceededException:
                if attempt == max_retries - 1:
                    raise
                sleep_time = backoff_factor ** attempt * 2
                logger.warning(f"Rate limit exceeded. Retrying in {sleep_time}s...")
                time.sleep(sleep_time)
            except GithubException as e:
                if e.status in (403, 429) and attempt < max_retries - 1:
                    sleep_time = backoff_factor ** attempt * 2
                    logger.warning(f"GitHub API status {e.status}. Retrying in {sleep_time}s...")
                    time.sleep(sleep_time)
                else:
                    raise

    def list_open_issues(self, repo_name: Optional[str] = None, bypass_cache: bool = False) -> List[Dict[str, Any]]:
        """
        Return open issues across all user repositories or a specified repository.
        Excludes Pull Requests and iterates through all pages.
        """
        cache_key = f"open_issues:{repo_name or 'ALL'}"
        if not bypass_cache and cache_key in self._cache:
            entry = self._cache[cache_key]
            if time.time() - entry["timestamp"] < self.cache_ttl:
                return entry["data"]

        user = self._get_authenticated_user()

        def _fetch():
            if repo_name:
                target_repo = f"{user.login}/{repo_name}" if "/" not in repo_name else repo_name
                repos = [self.gh.get_repo(target_repo)]
            else:
                repos = list(user.get_repos())

            results = []
            for repo in repos:
                # get_issues returns a PaginatedList, iterating handles pagination
                for issue in repo.get_issues(state="open"):
                    if issue.pull_request is None:  # Exclude PRs
                        results.append({
                            "repo": repo.name,
                            "full_name": repo.full_name,
                            "number": issue.number,
                            "title": issue.title,
                            "body": issue.body or "",
                            "state": issue.state,
                            "labels": [l.name for l in issue.labels],
                            "updated_at": issue.updated_at.isoformat() if issue.updated_at else None,
                            "url": issue.html_url
                        })
            return results

        results = self._call_with_retry(_fetch)
        self._cache[cache_key] = {"data": results, "timestamp": time.time()}
        return results

    def list_repositories(self) -> List[Dict[str, Any]]:
        """
        Return all repositories accessible by the authenticated user.
        """
        user = self._get_authenticated_user()

        def _fetch():
            repos = list(user.get_repos())
            return [
                {
                    "name": repo.name,
                    "full_name": repo.full_name,
                    "private": repo.private,
                    "description": repo.description or "",
                    "url": repo.html_url,
                    "stars": repo.stargazers_count,
                    "language": repo.language or "Unknown"
                }
                for repo in repos
            ]

        return self._call_with_retry(_fetch)

    def search_issues(self, keyword: str, repo_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Keyword and title search across open issues.
        """
        all_issues = self.list_open_issues(repo_name)
        keyword_lower = keyword.lower()
        return [
            i for i in all_issues
            if keyword_lower in i["title"].lower() or keyword_lower in i["body"].lower()
        ]

    def create_issue(self, repo_name: str, title: str, body: str = "", labels: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Create a new GitHub issue with idempotency check to prevent duplicates on retries.
        """
        user = self._get_authenticated_user()
        target_repo = f"{user.login}/{repo_name}" if "/" not in repo_name else repo_name

        def _execute():
            repo = self.gh.get_repo(target_repo)
            # Idempotency check: search open issues for exact title match
            for issue in repo.get_issues(state="open"):
                if issue.pull_request is None and issue.title.strip().lower() == title.strip().lower():
                    return {
                        "status": "already_exists",
                        "repo": repo.name,
                        "number": issue.number,
                        "title": issue.title,
                        "url": issue.html_url,
                        "message": f"Issue with title '{title}' already exists."
                    }

            kwargs = {"title": title, "body": body}
            if labels:
                kwargs["labels"] = labels

            issue = repo.create_issue(**kwargs)
            # Invalidate cached issues for this repo
            self._invalidate_cache(repo.name)
            return {
                "status": "created",
                "repo": repo.name,
                "number": issue.number,
                "title": issue.title,
                "url": issue.html_url
            }

        return self._call_with_retry(_execute)

    def add_label(self, repo_name: str, issue_number: int, label: str) -> Dict[str, Any]:
        """
        Add a label to an issue.
        """
        user = self._get_authenticated_user()
        target_repo = f"{user.login}/{repo_name}" if "/" not in repo_name else repo_name

        def _execute():
            repo = self.gh.get_repo(target_repo)
            issue = repo.get_issue(issue_number)
            issue.add_to_labels(label)
            self._invalidate_cache(repo.name)
            return {
                "status": "success",
                "repo": repo.name,
                "issue_number": issue_number,
                "label_added": label,
                "current_labels": [l.name for l in issue.labels]
            }

        return self._call_with_retry(_execute)

    def close_issue(self, repo_name: str, issue_number: int, comment: Optional[str] = None) -> Dict[str, Any]:
        """
        Close an existing open issue.
        """
        user = self._get_authenticated_user()
        target_repo = f"{user.login}/{repo_name}" if "/" not in repo_name else repo_name

        def _execute():
            repo = self.gh.get_repo(target_repo)
            issue = repo.get_issue(issue_number)
            if comment:
                issue.create_comment(comment)
            issue.edit(state="closed")
            self._invalidate_cache(repo.name)
            return {
                "status": "closed",
                "repo": repo.name,
                "issue_number": issue_number,
                "state": "closed"
            }

        return self._call_with_retry(_execute)

    def find_stale_issues(self, repo_name: str, days_inactive: int = 30) -> List[Dict[str, Any]]:
        """
        Find open issues with no update for `days_inactive` days.
        """
        user = self._get_authenticated_user()
        target_repo = f"{user.login}/{repo_name}" if "/" not in repo_name else repo_name

        def _execute():
            repo = self.gh.get_repo(target_repo)
            cutoff = datetime.now(timezone.utc) - timedelta(days=days_inactive)
            stale = []
            # Issues sorted ascending by updated date
            for issue in repo.get_issues(state="open", sort="updated", direction="asc"):
                if issue.pull_request is not None:
                    continue
                issue_updated = issue.updated_at.replace(tzinfo=timezone.utc)
                if issue_updated < cutoff:
                    stale.append({
                        "number": issue.number,
                        "title": issue.title,
                        "last_updated": issue.updated_at.isoformat(),
                        "url": issue.html_url
                    })
                else:
                    # Ascending order, so remaining issues are newer than cutoff
                    break
            return stale

        return self._call_with_retry(_execute)

    def bulk_add_label(self, repo_name: str, issue_numbers: List[int], label: str) -> List[Dict[str, Any]]:
        """
        Apply a label to multiple issues.
        """
        user = self._get_authenticated_user()
        target_repo = f"{user.login}/{repo_name}" if "/" not in repo_name else repo_name

        def _execute():
            repo = self.gh.get_repo(target_repo)
            results = []
            for number in issue_numbers:
                try:
                    issue = repo.get_issue(number)
                    issue.add_to_labels(label)
                    results.append({"number": number, "status": "labeled"})
                except Exception as e:
                    results.append({"number": number, "status": "failed", "error": str(e)})
            self._invalidate_cache(repo.name)
            return results

        return self._call_with_retry(_execute)

    def get_issue(self, repo_name: str, issue_number: int) -> Dict[str, Any]:
        """
        Fetch a single issue details.
        """
        user = self._get_authenticated_user()
        target_repo = f"{user.login}/{repo_name}" if "/" not in repo_name else repo_name

        def _execute():
            repo = self.gh.get_repo(target_repo)
            issue = repo.get_issue(issue_number)
            return {
                "repo": repo.name,
                "number": issue.number,
                "title": issue.title,
                "body": issue.body or "",
                "state": issue.state,
                "labels": [l.name for l in issue.labels],
                "url": issue.html_url
            }

        return self._call_with_retry(_execute)

    def get_rate_limit_status(self) -> Dict[str, Any]:
        """
        Retrieve core rate limit quota and reset time.
        """
        if not self.gh:
            raise ValueError("GITHUB_TOKEN is not configured.")
        
        def _execute():
            rate = self.gh.get_rate_limit().rate
            return {
                "limit": rate.limit,
                "remaining": rate.remaining,
                "reset_timestamp": rate.reset.isoformat() if rate.reset else None,
                "used": rate.limit - rate.remaining
            }

        return self._call_with_retry(_execute)

    def _invalidate_cache(self, repo_name: Optional[str] = None):
        if repo_name:
            keys_to_del = [k for k in self._cache if repo_name in k or "ALL" in k]
            for k in keys_to_del:
                del self._cache[k]
        else:
            self._cache.clear()
