import pytest
from unittest.mock import MagicMock, patch
from github_client import GitHubClient


@pytest.fixture
def mock_github_client():
    """Fixture providing a mocked GitHubClient instance for isolated unit tests."""
    with patch("github_client.Github"), patch("github_client.load_dotenv"), \
         patch.dict("os.environ", {"GITHUB_TOKEN": "fake_test_token_12345"}):
        gh_client = GitHubClient(token="fake_test_token_12345")
        
        # Mock PyGithub underlying objects
        mock_user = MagicMock()
        mock_user.login = "testuser"
        gh_client.user = mock_user
        gh_client._user_cache = mock_user
        gh_client.gh = MagicMock()
        
        return gh_client
