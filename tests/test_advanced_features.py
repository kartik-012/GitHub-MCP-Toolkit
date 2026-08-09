"""
Advanced Feature Unit Tests — GitHub MCP Toolkit
=================================================
Tests for: VectorEngine, TransactionJournal, PolicyEngine, CircuitBreaker.
These modules have zero test coverage in the core test suite — this file
closes that gap completely.
"""

import os
import json
import time
import tempfile
import pytest
from unittest.mock import patch

from vector_engine import VectorEngine
from transaction_journal import TransactionJournal
from policy_engine import PolicyEngine
from circuit_breaker import CircuitBreaker, CircuitBreakerOpenError, CircuitState


# ===========================================================================
# VectorEngine Tests
# ===========================================================================

class TestVectorEngine:

    def test_identical_texts_score_one(self):
        score = VectorEngine.compute_cosine_similarity("login crash bug", "login crash bug")
        assert score == 1.0

    def test_completely_different_texts_score_zero(self):
        score = VectorEngine.compute_cosine_similarity("login crash", "database migration")
        assert score == 0.0

    def test_similar_texts_score_between_zero_and_one(self):
        score = VectorEngine.compute_cosine_similarity(
            "authentication failure login",
            "login error authentication crash",
        )
        assert 0.0 < score < 1.0

    def test_empty_string_returns_zero(self):
        assert VectorEngine.compute_cosine_similarity("", "login crash") == 0.0
        assert VectorEngine.compute_cosine_similarity("login crash", "") == 0.0
        assert VectorEngine.compute_cosine_similarity("", "") == 0.0

    def test_stop_word_only_text_returns_zero(self):
        # "the and or" are all stop words — tokenize returns empty list
        score = VectorEngine.compute_cosine_similarity("the and or", "is of to")
        assert score == 0.0

    def test_rank_documents_returns_top_k(self):
        docs = [
            {"title": "login crash on startup", "body": "app fails at login", "number": 1, "url": "u1"},
            {"title": "dark mode rendering issue", "body": "ui glitch in dark mode", "number": 2, "url": "u2"},
            {"title": "database connection timeout", "body": "postgres timeout error", "number": 3, "url": "u3"},
            {"title": "login page not loading", "body": "authentication error", "number": 4, "url": "u4"},
        ]
        results = VectorEngine.rank_documents("login authentication error", docs, top_k=2)
        assert len(results) == 2
        # Both top results should be login-related
        titles = [r["title"] for r in results]
        assert any("login" in t for t in titles)

    def test_rank_documents_sorted_descending(self):
        docs = [
            {"title": "login failure crash", "body": "auth error", "number": 1, "url": "u1"},
            {"title": "login issue", "body": "", "number": 2, "url": "u2"},
        ]
        results = VectorEngine.rank_documents("login crash", docs, top_k=5)
        scores = [r["similarity_score"] for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_rank_documents_empty_corpus(self):
        results = VectorEngine.rank_documents("anything", [], top_k=5)
        assert results == []

    def test_similarity_score_attached_to_result(self):
        docs = [{"title": "login crash", "body": "auth fail", "number": 1, "url": "u"}]
        results = VectorEngine.rank_documents("login crash", docs, top_k=1)
        assert "similarity_score" in results[0]
        assert isinstance(results[0]["similarity_score"], float)


# ===========================================================================
# TransactionJournal Tests
# ===========================================================================

class TestTransactionJournal:

    @pytest.fixture(autouse=True)
    def _tmp_journal(self, tmp_path, monkeypatch):
        """Redirect JOURNAL_FILE to a temp path for isolation."""
        journal_path = str(tmp_path / "test_transactions.json")
        monkeypatch.setattr("transaction_journal.JOURNAL_FILE", journal_path)

    def test_record_and_retrieve_transaction(self):
        tx_id = TransactionJournal.record_transaction(
            tool="create_issue",
            repo_name="my-repo",
            parameters={"title": "Bug X", "body": "desc"},
            compensation={"type": "close_issue", "issue_number": 42},
        )
        assert tx_id.startswith("tx_")
        last = TransactionJournal.get_last_committed_transaction()
        assert last is not None
        assert last["tx_id"] == tx_id
        assert last["status"] == "committed"

    def test_mark_reverted(self):
        tx_id = TransactionJournal.record_transaction(
            tool="add_label",
            repo_name="repo",
            parameters={"label": "bug"},
            compensation={"type": "remove_label", "issue_number": 5, "label": "bug"},
        )
        TransactionJournal.mark_reverted(tx_id)
        history = TransactionJournal.list_history(limit=5)
        found = next(r for r in history if r["tx_id"] == tx_id)
        assert found["status"] == "reverted"
        assert "reverted_at" in found

    def test_empty_journal_returns_none(self):
        result = TransactionJournal.get_last_committed_transaction()
        assert result is None

    def test_list_history_most_recent_first(self):
        TransactionJournal.record_transaction(
            "create_issue", "r", {"title": "first"}, {"type": "close_issue", "issue_number": 1}
        )
        TransactionJournal.record_transaction(
            "create_issue", "r", {"title": "second"}, {"type": "close_issue", "issue_number": 2}
        )
        history = TransactionJournal.list_history(limit=10)
        assert history[0]["parameters"]["title"] == "second"
        assert history[1]["parameters"]["title"] == "first"

    def test_skips_reverted_for_last_committed(self):
        tx1 = TransactionJournal.record_transaction(
            "create_issue", "r", {"title": "A"}, {"type": "close_issue", "issue_number": 1}
        )
        tx2 = TransactionJournal.record_transaction(
            "create_issue", "r", {"title": "B"}, {"type": "close_issue", "issue_number": 2}
        )
        TransactionJournal.mark_reverted(tx2)
        last = TransactionJournal.get_last_committed_transaction()
        assert last["tx_id"] == tx1


# ===========================================================================
# PolicyEngine Tests
# ===========================================================================

class TestPolicyEngine:

    BASE_POLICY = {
        "allow_writes": True,
        "min_rate_limit_remaining": 20,
        "restricted_labels": ["security-critical", "production-outage"],
        "max_bulk_limit": 15,
    }

    def _evaluate(self, tool, args, rate_remaining=None, policy_override=None):
        policy = {**self.BASE_POLICY, **(policy_override or {})}
        with patch.object(PolicyEngine, "load_policy", return_value=policy):
            return PolicyEngine.evaluate(tool, args, rate_limit_remaining=rate_remaining)

    def test_normal_write_allowed(self):
        allowed, reason = self._evaluate("create_issue", {"repo_name": "r", "title": "t"})
        assert allowed is True

    def test_global_write_freeze_blocks_writes(self):
        allowed, reason = self._evaluate(
            "create_issue", {}, policy_override={"allow_writes": False}
        )
        assert allowed is False
        assert "frozen" in reason.lower()

    def test_global_write_freeze_allows_reads(self):
        allowed, _ = self._evaluate(
            "get_open_issues", {}, policy_override={"allow_writes": False}
        )
        assert allowed is True

    def test_rate_limit_below_buffer_blocks(self):
        allowed, reason = self._evaluate("create_issue", {}, rate_remaining=5)
        assert allowed is False
        assert "quota" in reason.lower() or "buffer" in reason.lower()

    def test_rate_limit_above_buffer_allowed(self):
        allowed, _ = self._evaluate("create_issue", {}, rate_remaining=100)
        assert allowed is True

    def test_restricted_label_blocked(self):
        allowed, reason = self._evaluate("add_label", {"label": "security-critical"})
        assert allowed is False
        assert "restricted" in reason.lower()

    def test_non_restricted_label_allowed(self):
        allowed, _ = self._evaluate("add_label", {"label": "bug"})
        assert allowed is True

    def test_bulk_within_limit_allowed(self):
        allowed, _ = self._evaluate("bulk_label_stale_issues", {"affected_count": 10})
        assert allowed is True

    def test_bulk_exceeds_limit_blocked(self):
        allowed, reason = self._evaluate("bulk_label_stale_issues", {"affected_count": 20})
        assert allowed is False
        assert "bulk" in reason.lower() or "limit" in reason.lower()


# ===========================================================================
# CircuitBreaker Tests
# ===========================================================================

class TestCircuitBreaker:

    def make_breaker(self, threshold=3, cooldown=60.0):
        return CircuitBreaker("test", failure_threshold=threshold, cooldown_seconds=cooldown)

    def test_initial_state_is_closed(self):
        cb = self.make_breaker()
        assert cb.state == CircuitState.CLOSED

    def test_successful_call_keeps_closed(self):
        cb = self.make_breaker()
        result = cb.call(lambda: "ok")
        assert result == "ok"
        assert cb.state == CircuitState.CLOSED

    def test_failures_below_threshold_stay_closed(self):
        cb = self.make_breaker(threshold=3)
        for _ in range(2):
            with pytest.raises(RuntimeError):
                cb.call(lambda: (_ for _ in ()).throw(RuntimeError("fail")))
        assert cb.state == CircuitState.CLOSED

    def test_failures_at_threshold_open_circuit(self):
        cb = self.make_breaker(threshold=3)
        for _ in range(3):
            with pytest.raises(Exception):
                cb.call(lambda: (_ for _ in ()).throw(RuntimeError("fail")))
        assert cb.state == CircuitState.OPEN

    def test_open_circuit_fast_fails(self):
        cb = self.make_breaker(threshold=1)
        with pytest.raises(Exception):
            cb.call(lambda: (_ for _ in ()).throw(RuntimeError("initial fail")))
        assert cb.state == CircuitState.OPEN
        with pytest.raises(CircuitBreakerOpenError):
            cb.call(lambda: "should never run")

    def test_cooldown_transitions_to_half_open(self):
        cb = self.make_breaker(threshold=1, cooldown=0.1)
        with pytest.raises(Exception):
            cb.call(lambda: (_ for _ in ()).throw(RuntimeError("fail")))
        time.sleep(0.15)
        assert cb.state == CircuitState.HALF_OPEN

    def test_half_open_success_closes_circuit(self):
        cb = self.make_breaker(threshold=1, cooldown=0.1)
        with pytest.raises(Exception):
            cb.call(lambda: (_ for _ in ()).throw(RuntimeError("fail")))
        time.sleep(0.15)
        assert cb.state == CircuitState.HALF_OPEN
        cb.call(lambda: "probe ok")
        assert cb.state == CircuitState.CLOSED
        assert cb._failure_count == 0

    def test_half_open_failure_reopens_circuit(self):
        cb = self.make_breaker(threshold=1, cooldown=0.1)
        with pytest.raises(Exception):
            cb.call(lambda: (_ for _ in ()).throw(RuntimeError("fail")))
        time.sleep(0.15)
        assert cb.state == CircuitState.HALF_OPEN
        with pytest.raises(Exception):
            cb.call(lambda: (_ for _ in ()).throw(RuntimeError("probe fail")))
        assert cb.state == CircuitState.OPEN

    def test_manual_reset_closes_circuit(self):
        cb = self.make_breaker(threshold=1)
        with pytest.raises(Exception):
            cb.call(lambda: (_ for _ in ()).throw(RuntimeError("fail")))
        assert cb.state == CircuitState.OPEN
        cb.reset()
        assert cb.state == CircuitState.CLOSED
        assert cb._failure_count == 0

    def test_status_returns_all_fields(self):
        cb = self.make_breaker()
        status = cb.status()
        assert "name" in status
        assert "state" in status
        assert "failure_count" in status
        assert "failure_threshold" in status
        assert "cooldown_seconds" in status


# ===========================================================================
# Tracer Tests
# ===========================================================================

class TestTracer:

    def test_span_records_duration(self):
        from tracer import Span
        s = Span("test_span")
        time.sleep(0.01)
        s.finish(status="ok")
        assert s.duration_ms is not None
        assert s.duration_ms > 0
        assert s.status == "ok"

    def test_span_to_dict_structure(self):
        from tracer import Span
        s = Span("my_phase")
        s.finish(status="pass", extra_key="extra_value")
        d = s.to_dict()
        assert d["span"] == "my_phase"
        assert d["status"] == "pass"
        assert "duration_ms" in d
        assert d["meta"]["extra_key"] == "extra_value"

    def test_trace_context_manager_auto_finishes_span(self):
        from tracer import Trace
        t = Trace("test_tool")
        with t.span("auto_phase") as s:
            pass  # no explicit s.finish()
        assert s.status == "ok"
        assert s.duration_ms is not None

    def test_trace_commit_writes_to_file(self, tmp_path, monkeypatch):
        import tracer as tracer_mod
        from tracer import Trace
        trace_file = str(tmp_path / "test_traces.jsonl")
        monkeypatch.setattr(tracer_mod, "TRACES_FILE", trace_file)

        t = Trace("commit_test")
        with t.span("phase_a") as s:
            s.finish(status="done")
        t.commit(outcome="success")

        with open(trace_file, "r") as f:
            lines = f.readlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["tool"] == "commit_test"
        assert record["outcome"] == "success"
        assert len(record["spans"]) == 1
        assert record["spans"][0]["span"] == "phase_a"

    def test_read_recent_traces_returns_newest_first(self, tmp_path, monkeypatch):
        import tracer as tracer_mod
        from tracer import Trace, read_recent_traces
        trace_file = str(tmp_path / "test_traces.jsonl")
        monkeypatch.setattr(tracer_mod, "TRACES_FILE", trace_file)

        t1 = Trace("tool_first")
        t1.commit(outcome="ok")
        t2 = Trace("tool_second")
        t2.commit(outcome="ok")

        results = read_recent_traces(limit=10)
        assert len(results) == 2
        assert results[0]["tool"] == "tool_second"  # newest first
        assert results[1]["tool"] == "tool_first"


# ===========================================================================
# Schema Validation Tests
# ===========================================================================

class TestSchemas:

    def test_validate_success_returns_clean_dict(self):
        from schemas import validate, RateLimitResponse
        data = {"limit": 5000, "remaining": 4990, "used": 10, "reset_timestamp": "2026-08-09T12:00:00Z"}
        result = validate(RateLimitResponse, data)
        assert result["limit"] == 5000
        assert result["remaining"] == 4990
        assert result["used"] == 10

    def test_validate_strips_none_fields(self):
        from schemas import validate, CreateIssueResponse
        data = {"status": "created", "repo": "my-repo", "number": 1, "title": "Bug", "url": "http://u"}
        result = validate(CreateIssueResponse, data)
        assert "transaction_id" not in result  # None fields excluded
        assert "similarity_score" not in result

    def test_validate_failure_returns_error_dict(self):
        from schemas import validate, RateLimitResponse
        bad_data = {"limit": "not_a_number", "remaining": 10, "used": 5}
        result = validate(RateLimitResponse, bad_data)
        assert result["status"] == "schema_validation_error"
        assert result["schema"] == "RateLimitResponse"
        assert "errors" in result

    def test_validate_issue_list_schema(self):
        from schemas import validate, IssueListResponse
        data = {
            "status": "success",
            "total": 1,
            "issues": [{"repo": "r", "number": 1, "title": "t", "state": "open", "labels": [], "url": "u"}]
        }
        result = validate(IssueListResponse, data)
        assert result["total"] == 1
        assert len(result["issues"]) == 1


# ===========================================================================
# list_repositories GitHubClient Method Tests
# ===========================================================================

class TestListRepositories:

    def test_list_repositories_returns_list(self, monkeypatch):
        from unittest.mock import MagicMock, patch
        with patch("github_client.Github"), patch("github_client.load_dotenv"), \
             patch.dict("os.environ", {"GITHUB_TOKEN": "fake"}):
            from github_client import GitHubClient
            client = GitHubClient(token="fake")
            mock_user = MagicMock()
            mock_user.login = "testuser"
            client._user_cache = mock_user
            client.gh = MagicMock()

            mock_repo = MagicMock()
            mock_repo.name = "test-repo"
            mock_repo.full_name = "testuser/test-repo"
            mock_repo.private = False
            mock_repo.description = "A test repo"
            mock_repo.html_url = "https://github.com/testuser/test-repo"
            mock_repo.stargazers_count = 5
            mock_repo.language = "Python"
            mock_user.get_repos.return_value = [mock_repo]

            repos = client.list_repositories()
            assert len(repos) == 1
            assert repos[0]["name"] == "test-repo"
            assert repos[0]["private"] is False
            assert repos[0]["stars"] == 5

    def test_list_repositories_empty(self, monkeypatch):
        from unittest.mock import MagicMock, patch
        with patch("github_client.Github"), patch("github_client.load_dotenv"), \
             patch.dict("os.environ", {"GITHUB_TOKEN": "fake"}):
            from github_client import GitHubClient
            client = GitHubClient(token="fake")
            mock_user = MagicMock()
            mock_user.login = "testuser"
            client._user_cache = mock_user
            client.gh = MagicMock()
            mock_user.get_repos.return_value = []

            repos = client.list_repositories()
            assert repos == []

    def test_list_repositories_tool_returns_dict(self, monkeypatch):
        """Verify the tool returns Dict[str, Any], not str."""
        from unittest.mock import MagicMock, patch
        try:
            from fastmcp import FastMCP
        except ImportError:
            from mcp.server.fastmcp import FastMCP

        mcp = FastMCP("test")
        mock_gh = MagicMock()
        mock_gh.list_repositories.return_value = [
            {"name": "r", "full_name": "u/r", "private": False,
             "description": "", "url": "http://u", "stars": 0, "language": "Python"}
        ]

        from tools import list_repositories
        list_repositories.register(mcp, mock_gh)

        # Get the registered function
        import asyncio
        if hasattr(mcp, "_tool_manager") and hasattr(mcp._tool_manager, "_tools"):
            fn = mcp._tool_manager._tools["list_repositories"].fn
        elif hasattr(mcp, "get_tool"):
            tool_obj = asyncio.run(mcp.get_tool("list_repositories"))
            fn = tool_obj.fn
        else:
            pytest.skip("Cannot extract tool function from this FastMCP version")

        result = fn()
        assert isinstance(result, dict)
        assert result["status"] == "success"
        assert result["total"] == 1
        assert "repositories" in result
