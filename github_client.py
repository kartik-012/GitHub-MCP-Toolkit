import os
import time
import logging
import itertools
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv
from github import Github, Auth, GithubException, RateLimitExceededException
from circuit_breaker import CircuitBreaker, CircuitBreakerOpenError

# Ensure .env is loaded from the directory this script lives in
env_path = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(dotenv_path=env_path)

logger = logging.getLogger("github_mcp_toolkit")


class GitHubClient:
    """
    Production-grade GitHub API client wrapper for MCP server.
    Implements caching, PR filtering, rate limit handling with exponential
    backoff, lazy bounded pagination iteration, write idempotency checks,
    explicit socket timeouts, and a Circuit Breaker that trips after 3
    consecutive failures to prevent cascading API errors from reaching the LLM.
    """

    DEFAULT_TIMEOUT_SECONDS = 15
    DEFAULT_PER_PAGE = 30

    def __init__(self, token: Optional[str] = None, timeout: int = DEFAULT_TIMEOUT_SECONDS):
        self.token = token or os.getenv("GITHUB_TOKEN")
        self.timeout = timeout
        if not self.token:
            # Fallback/mock mode notice if token not provided, raised on real API access
            self.gh = None
            self.user = None
        else:
            auth = Auth.Token(self.token)
            self.gh = Github(auth=auth, timeout=self.timeout, per_page=self.DEFAULT_PER_PAGE)
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

    @staticmethod
    def parse_repo_identifier(repo_name: str, default_owner: Optional[str] = None) -> str:
        """
        Normalize repository identifier into 'owner/repo' format.
        Trims whitespace, strips leading/trailing slashes, and prepends default_owner if single token.
        """
        cleaned = (repo_name or "").strip().strip("/")
        if "/" not in cleaned and default_owner:
            return f"{default_owner}/{cleaned}"
        return cleaned

    def clear_cache(self, prefix: Optional[str] = None) -> int:
        """Clear cached API responses, optionally matching a specific cache key prefix."""
        if prefix is None:
            count = len(self._cache)
            self._cache.clear()
            return count
        keys_to_remove = [k for k in list(self._cache.keys()) if k.startswith(prefix)]
        for k in keys_to_remove:
            del self._cache[k]
        return len(keys_to_remove)

    def _get_authenticated_user(self):
        """Retrieve and cache the authenticated GitHub user profile, validating token presence."""
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

    def list_open_issues(
        self,
        repo_name: Optional[str] = None,
        bypass_cache: bool = False,
        max_results: int = 100,
        max_repos: int = 20,
    ) -> List[Dict[str, Any]]:
        """
        Return open issues across user repositories or a specified repository.
        Excludes Pull Requests and enforces lazy max_results / max_repos safety bounds.
        """
        cache_key = f"open_issues:{repo_name or 'ALL'}:{max_results}:{max_repos}"
        if not bypass_cache and cache_key in self._cache:
            entry = self._cache[cache_key]
            if time.time() - entry["timestamp"] < self.cache_ttl:
                return entry["data"]

        user = self._get_authenticated_user()

        def _fetch():
            if repo_name:
                target_repo = f"{user.login}/{repo_name}" if "/" not in repo_name else repo_name
                repos_iter = [self.gh.get_repo(target_repo)]
            else:
                # Lazy slice over PaginatedList so PyGithub stops fetching pages at max_repos
                repos_iter = itertools.islice(user.get_repos(), max_repos)

            results = []
            for repo in repos_iter:
                # get_issues returns a PaginatedList; break early once max_results reached
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
                        if len(results) >= max_results:
                            return results
            return results

        results = self._call_with_retry(_fetch)
        self._cache[cache_key] = {"data": results, "timestamp": time.time()}
        return results

    def list_repositories(self, max_repos: int = 50) -> List[Dict[str, Any]]:
        """
        Return repositories accessible by the authenticated user with bounded pagination.
        """
        user = self._get_authenticated_user()

        def _fetch():
            repos_iter = itertools.islice(user.get_repos(), max_repos)
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
                for repo in repos_iter
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

    def create_issue(
        self,
        repo_name: str,
        title: str,
        body: str = "",
        labels: Optional[List[str]] = None,
        max_idempotency_scan: int = 100,
    ) -> Dict[str, Any]:
        """
        Create a new GitHub issue with bounded idempotency check to prevent duplicates on retries.
        """
        user = self._get_authenticated_user()
        target_repo = f"{user.login}/{repo_name}" if "/" not in repo_name else repo_name

        def _execute():
            repo = self.gh.get_repo(target_repo)
            # Bounded idempotency check: scan up to max_idempotency_scan recent open issues
            for issue in itertools.islice(repo.get_issues(state="open"), max_idempotency_scan):
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

    def reopen_issue(self, repo_name: str, issue_number: int) -> Dict[str, Any]:
        """
        Reopen a closed issue (used by Saga compensation rollback).
        """
        user = self._get_authenticated_user()
        target_repo = f"{user.login}/{repo_name}" if "/" not in repo_name else repo_name

        def _execute():
            repo = self.gh.get_repo(target_repo)
            issue = repo.get_issue(issue_number)
            issue.edit(state="open")
            self._invalidate_cache(repo.name)
            return {
                "status": "reopened",
                "repo": repo.name,
                "issue_number": issue_number,
                "state": "open"
            }

        return self._call_with_retry(_execute)

    def remove_label(self, repo_name: str, issue_number: int, label: str) -> Dict[str, Any]:
        """
        Remove a label from an issue (used by Saga compensation rollback).
        """
        user = self._get_authenticated_user()
        target_repo = f"{user.login}/{repo_name}" if "/" not in repo_name else repo_name

        def _execute():
            repo = self.gh.get_repo(target_repo)
            issue = repo.get_issue(issue_number)
            issue.remove_from_labels(label)
            self._invalidate_cache(repo.name)
            return {
                "status": "label_removed",
                "repo": repo.name,
                "issue_number": issue_number,
                "label": label
            }

        return self._call_with_retry(_execute)

    def find_stale_issues(self, repo_name: str, days_inactive: int = 30, max_results: int = 100) -> List[Dict[str, Any]]:
        """
        Find open issues with no update for `days_inactive` days (bounded by max_results).
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
                    if len(stale) >= max_results:
                        break
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

        res = self._call_with_retry(_execute)
        self._cache["rate_limit"] = {"data": res, "timestamp": time.time()}
        return res

    def get_cached_rate_remaining(self) -> Optional[int]:
        """
        Return cached GitHub API remaining quota (int) for PolicyEngine evaluation.
        Returns None if unavailable or unconfigured in mock environments.
        """
        if "rate_limit" in self._cache:
            entry = self._cache["rate_limit"]
            if time.time() - entry["timestamp"] < self.cache_ttl:
                rem = entry["data"].get("remaining")
                if isinstance(rem, int):
                    return rem
        try:
            status = self.get_rate_limit_status()
            rem = status.get("remaining")
            return rem if isinstance(rem, int) else None
        except Exception:
            return None

    def _invalidate_cache(self, repo_name: Optional[str] = None):
        if repo_name:
            keys_to_del = [k for k in self._cache if repo_name in k or "ALL" in k]
            for k in keys_to_del:
                del self._cache[k]
        else:
            self._cache.clear()
