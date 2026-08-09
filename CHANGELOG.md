# Changelog — `github-mcp-toolkit`

All notable changes to this project will be documented in this file.

## [2.0.0] - 2026-08-09

### Added
- **Circuit Breaker Pattern**: `circuit_breaker.py` wraps all GitHub API calls. After 3 consecutive failures, the circuit trips to OPEN and fast-fails for 60s. States: CLOSED → OPEN → HALF_OPEN → CLOSED.
- **Pydantic Response Schemas**: `schemas.py` — typed contracts for all 12 tool responses, validated via `validate()` helper. Active in `create_issue`, `get_rate_limit_status`.
- **Execution Tracer**: `tracer.py` — zero-dependency, OpenTelemetry-inspired span-based tracer. Records per-phase timing (policy_check, vector_dedup, github_api, saga_journal) to `traces.jsonl`.
- **TF-IDF Vector Engine**: `vector_engine.py` — cosine similarity engine for semantic duplicate detection in `create_issue` and semantic search in `semantic_search_issues`.
- **Saga Rollback Journal**: `transaction_journal.py` — compensating undo for write mutations across `create_issue`, `add_label`, and `close_issue`.
- **ABAC Policy Engine**: `policy_engine.py` — declarative rules (write freeze, quota buffer, restricted labels, bulk cap) enforced before `create_issue`, `add_label`, `close_issue`, and `bulk_label_stale_issues`.
- **Adversarial Eval Harness**: 20 adversarial test cases (`eval/adversarial_queries.json`) covering prompt injection, semantic confusion, and multi-intent attacks.
- **Advanced Test Suite**: `tests/test_advanced_features.py` — 33+ unit tests for VectorEngine, TransactionJournal, PolicyEngine, CircuitBreaker, Tracer, and schemas.
- **4 Advanced Tools**: `semantic_search_issues`, `undo_last_action`, `get_transaction_history`, `get_trace_history`.
- **`list_repositories` tool**: Lists all accessible GitHub repositories.

### Changed
- **Circuit Breaker** moved from class-level singleton to per-instance variable in `GitHubClient` to prevent test pollution and improve isolation.
- **`_call_with_retry`** simplified — removed TOCTOU double-check and encapsulation-breaking `_on_failure()` call.
- **`add_label`** and **`close_issue`** now integrate PolicyEngine checks and Saga compensation recording, matching the depth of `create_issue`.
- **Tool Selection Evaluation**: Standard accuracy 100% (50/50), Adversarial accuracy 100% (20/20).
- **Total Test Count**: 44+ unit + integration tests across 4 test files.

### Security
- Removed real GitHub PAT from `.env.example` — replaced with placeholder.

---

## [1.0.0] - 2026-08-09

### Added
- **Core Server**: FastMCP server (`server.py`) registering 8 tools.
- **GitHub Client**: Production `GitHubClient` (`github_client.py`) with automatic pagination, PR filtering (`issue.pull_request is None`), exponential backoff on 403/429 rate limits, and 60s read caching.
- **2-Phase Preview-Token Guardrail**: `bulk_label_stale_issues` tool with SHA256 preview tokens and 5-min TTL expiration.
- **Untrusted Data Sandbox**: `triage_issue` tool using local Ollama model (`llama3.2:1b`) with prompt injection defense prompt wrapping and defensive JSON parser.
- **Rate Limit Inspector**: `get_rate_limit_status` tool exposing API quota metrics.
- **Evaluation Harness**: 50-query benchmark dataset (`eval/test_queries.json`) and automated evaluation runner (`eval/run_eval.py`) yielding 98.0% selection accuracy.
- **Unit Test Suite**: `pytest` suite covering PR filtering, idempotency, preview tokens, and prompt injection isolation.
- **Production Artifacts**: `Dockerfile`, `docker-compose.yml`, `README.md`, `DECISIONS.md`, `SECURITY.md`.
