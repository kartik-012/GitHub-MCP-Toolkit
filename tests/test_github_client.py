import pytest
from unittest.mock import MagicMock
from datetime import datetime, timezone, timedelta


def test_pr_filtering(mock_github_client):
    """Verify that Pull Request objects are strictly excluded from open issue counts."""
    mock_issue_real = MagicMock(
        pull_request=None,
        title="Real Open Issue",
        number=101,
        body="Issue description text",
        state="open",
        labels=[],
        updated_at=datetime.now(timezone.utc),
        html_url="https://github.com/testuser/repo1/issues/101"
    )
    mock_issue_pr = MagicMock(
        pull_request=MagicMock(),  # Non-None indicates a Pull Request
        title="Feature PR #102",
        number=102,
        body="PR description text",
        state="open"
    )

    mock_repo = MagicMock()
    mock_repo.name = "repo1"
    mock_repo.full_name = "testuser/repo1"
    mock_repo.get_issues.return_value = [mock_issue_real, mock_issue_pr]

    mock_github_client.gh.get_repo.return_value = mock_repo
    mock_github_client.user.get_repos.return_value = [mock_repo]

    results = mock_github_client.list_open_issues(repo_name="repo1", bypass_cache=True)

    assert len(results) == 1
    assert results[0]["number"] == 101
    assert results[0]["title"] == "Real Open Issue"


def test_create_issue_idempotency(mock_github_client):
    """Verify that creating an issue with an existing title triggers the idempotency return."""
    existing_issue = MagicMock(
        pull_request=None,
        title="Existing Bug Title",
        number=42,
        html_url="https://github.com/testuser/repo1/issues/42"
    )
    mock_repo = MagicMock()
    mock_repo.name = "repo1"
    mock_repo.get_issues.return_value = [existing_issue]

    mock_github_client.gh.get_repo.return_value = mock_repo

    result = mock_github_client.create_issue("repo1", "Existing Bug Title", "Some body")

    assert result["status"] == "already_exists"
    assert result["number"] == 42
    assert "already exists" in result["message"]
    # Ensure create_issue was NOT called on PyGithub repo
    mock_repo.create_issue.assert_not_called()


def test_search_issues(mock_github_client):
    """Verify keyword filtering across titles and bodies."""
    issue1 = {
        "repo": "repo1", "number": 1, "title": "Fix database connection timeout",
        "body": "Server crashes on DB timeout", "state": "open", "labels": [], "url": "http://url1"
    }
    issue2 = {
        "repo": "repo1", "number": 2, "title": "Update README docs",
        "body": "Instructions for local setup", "state": "open", "labels": [], "url": "http://url2"
    }

    mock_github_client.list_open_issues = MagicMock(return_value=[issue1, issue2])

    search_res = mock_github_client.search_issues("database")
    assert len(search_res) == 1
    assert search_res[0]["number"] == 1


def test_find_stale_issues(mock_github_client):
    """Verify inactive date cutoff filtering."""
    now = datetime.now(timezone.utc)
    old_date = now - timedelta(days=40)
    recent_date = now - timedelta(days=5)

    stale_issue = MagicMock(
        pull_request=None,
        number=5,
        title="Stale ticket",
        updated_at=old_date,
        html_url="http://stale"
    )
    active_issue = MagicMock(
        pull_request=None,
        number=6,
        title="Active ticket",
        updated_at=recent_date,
        html_url="http://active"
    )

    mock_repo = MagicMock()
    mock_repo.get_issues.return_value = [stale_issue, active_issue]
    mock_github_client.gh.get_repo.return_value = mock_repo

    stale_list = mock_github_client.find_stale_issues("repo1", days_inactive=30)
    assert len(stale_list) == 1
    assert stale_list[0]["number"] == 5


def test_get_rate_limit_status(mock_github_client):
    """Verify rate limit data structure parsing."""
    mock_rate = MagicMock()
    mock_rate.limit = 5000
    mock_rate.remaining = 4950
    mock_rate.reset = datetime.now(timezone.utc)

    mock_rate_limit_obj = MagicMock()
    mock_rate_limit_obj.rate = mock_rate
    mock_github_client.gh.get_rate_limit.return_value = mock_rate_limit_obj

    status = mock_github_client.get_rate_limit_status()
    assert status["limit"] == 5000
    assert status["remaining"] == 4950
    assert status["used"] == 50
