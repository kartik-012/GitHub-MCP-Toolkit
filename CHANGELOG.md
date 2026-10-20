# Changelog — `github-mcp-toolkit`

All notable changes to this project will be documented in this file.

## [3.0.1] - 2026-10-20

### Improved
- **Documentation & Security**: Added responsible vulnerability disclosure guidelines and comprehensive `.env.example` configurations.
- **Code Quality & Typing**: Refined Pydantic model field docstrings, tracer typing annotations, and circuit breaker state descriptions.

---

## [3.0.0] - 2026-10-12

### Added — Principal Engineer & Next-Horizon Enterprise Capabilities
- **Enterprise Outbound DLP + Shannon Entropy + Luhn PII Scanner (`policy_engine.py`)**: Expanded outbound secret detection to 10 vendor credential patterns (JWTs, Slack tokens, Database URIs, US SSNs, GitHub PATs, AWS keys, OpenAI/Anthropic keys, PEM blocks), Shannon Information Entropy ($H(X) \ge 3.8\text{ bits/char}$) for vendor-agnostic secrets, and ISO/IEC 7812 Luhn checksum validation for payment card numbers.
- **Parameter-Bound 2-Phase Preview Tokens for Single-Item Writes (`policy_engine.py`, `tools/`)**: Extended single-use cryptographic `SHA256` preview tokens to `create_issue`, `add_label`, and `close_issue`, blocking parameter bait-and-switch and concurrent replay attacks.
- **Tamper-Evident SHA-256 Hash-Chained Saga Journal (`transaction_journal.py`)**: Added cryptographic hash-chaining (`prev_hash`, `record_hash`, `verify_chain_integrity()`), `dry_run=True` compensation preview, and out-of-order targeted `tx_id` rollbacks in `undo_last_action`.
- **5-Signature Indirect Prompt-Injection Threat Scorer (`tools/triage_issue.py`)**: Added `detect_prompt_injection_signals()`, active `apply_labels=True` defense gate (`status="injection_blocked"`), SLA remediation playbooks (`SLA_TARGETS_HOURS`), and automatic related-issue cross-linking.
- **Hybrid RRF (Reciprocal Rank Fusion) Search Engine (`vector_engine.py`)**: Combined smoothed Corpus TF-IDF cosine similarity with BM25-style exact lexical/error-code/issue-number matching (`hybrid_rrf_search`).
- **Scoped Session RBAC & Per-Tool 60s Sliding-Window Velocity Limiter (`policy_engine.py`)**: Added `read_only`, `triage_operator`, `maintainer`, and `admin` personas (`check_rbac_permission`) plus thread-safe per-tool write velocity rate limiting (`check_and_record_velocity`).
- **Pre-Flight Blast-Radius, Quota Impact & Reversibility Simulator (`policy_engine.py`)**: Added `PolicyEngine.estimate_blast_radius()` wired into all 4 write tools' Phase-1 previews.
- **Context-Window Token Budgeter & Adaptive Payload Compactor (`schemas.py`, `server.py`)**: Added `estimate_tokens()` and `compact_and_budget_payload()` in the central server middleware (`DEFAULT_MAX_PAYLOAD_TOKENS = 3000`).
- **Deterministic Chaos & Fault-Injection Resilience Harness (`circuit_breaker.py`)**: Added `ChaosInjector` (`502_bad_gateway`, `503_unavailable`, `429_rate_limit`, `timeout`) integrated into `CircuitBreaker.call()`.
- **Full MCP Specification Coverage (`server.py`)**: Registered 4 native `@mcp.resource` endpoints (`github://policy/active`, `github://telemetry/slo`, `github://journal/integrity`, `github://compliance/snapshot`) and 2 `@mcp.prompt` workflow templates (`incident_triage_workflow`, `safe_bulk_cleanup_workflow`).
- **Live SLO (`p50`/`p95` Latency) & SOC-2 Compliance Snapshot Exporter**: Added `get_telemetry_summary()` in `tracer.py` and `PolicyEngine.export_compliance_snapshot()` with 64-char `snapshot_sha256` attestation.

### Changed
- **Test Suite Expanded to 110+ Tests**: Added comprehensive unit, multi-threaded concurrency, cryptographic tamper-detection, and chaos fault-injection tests in `tests/test_advanced_features.py`.
- **Evaluation Harness (`eval/run_eval.py`)**: Added `--llm` live Ollama evaluation mode alongside the deterministic + TF-IDF router (`80/80` Standard, `20/20` Adversarial) and full parity verification across all 13 tools, 4 resources, and 2 prompts.

---

## [2.0.0] - 2026-08-09

### Added
- **Circuit Breaker Pattern**: `circuit_breaker.py` wraps all GitHub API calls (`CLOSED → OPEN → HALF_OPEN → CLOSED`).
- **Pydantic Response Schemas**: `schemas.py` — typed contracts for all tool responses.
- **Execution Tracer**: `tracer.py` — zero-dependency, OpenTelemetry-inspired span-based tracer (`traces.jsonl`).
- **TF-IDF Vector Engine**: `vector_engine.py` — cosine similarity engine for semantic duplicate detection and search.
- **Saga Rollback Journal**: `transaction_journal.py` — compensating undo for write mutations.
- **ABAC Policy Engine**: `policy_engine.py` — declarative rules enforced before write operations.
- **Adversarial Eval Harness**: 20 adversarial test cases (`eval/adversarial_queries.json`).

---

## [1.0.0] - 2026-08-09

### Added
- **Core Server**: FastMCP server (`server.py`) registering core GitHub issue management tools, `GitHubClient` with pagination and rate-limit backoff, 2-phase preview tokens on `bulk_label_stale_issues`, and XML prompt-injection sandbox on `triage_issue`.
