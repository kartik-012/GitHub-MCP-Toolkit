# Security Specification — `github-mcp-toolkit` (v3.0 Enterprise Architecture)

## Threat Model & Multi-Layer Defense-in-Depth Posture

As autonomous AI agents gain write access to operational GitHub repositories, security guardrails must be enforced deterministically inside the MCP server layer — never delegated to LLM prompt adherence alone. `github-mcp-toolkit` implements **10 independent security and governance layers**.

---

## 1. Outbound DLP, Shannon Entropy & ISO/IEC 7812 Luhn PII Scanner (`policy_engine.py`)

### Vulnerability Pattern
An LLM summarizing crash logs or customer tickets can inadvertently exfiltrate credentials, API keys, database connection strings, or customer payment cards into public GitHub issues or comments.

### Mitigation
1. **10 Vendor Regex Signatures**: Blocks GitHub PATs (`ghp_`, `github_pat_`), AWS Access Key IDs (`AKIA`/`ASIA`), OpenAI/Anthropic API keys, PEM Private Keys, Bearer/Basic auth headers, JWTs (`eyJ...`), Slack tokens (`xox[baprs]-`), Database Connection URIs with embedded passwords (`postgresql://user:pass@host`), and US Social Security Numbers (SSNs).
2. **Shannon Information Entropy ($H(X) \ge 3.8\text{ bits/char}$)**: Computes character-level Shannon entropy across secret assignment values (`secret=...`, `token=...`, `password=...`, `api_key=...`) to block vendor-agnostic high-entropy secrets while ignoring low-entropy placeholders.
3. **ISO/IEC 7812 Luhn Checksum Validation**: Validates 13-to-19 digit candidate payment card sequences using the Luhn modulo-10 algorithm before blocking, preventing false positives on ordinary numerical IDs.

---

## 2. Parameter-Bound Cryptographic 2-Phase Preview Tokens (`policy_engine.py`, `tools/`)

### Vulnerability Pattern
A boolean `confirmed=True` parameter allows an LLM to hallucinate user approval or perform a parameter bait-and-switch (e.g., previewing issue `#10` and confirming on issue `#99`).

### Mitigation
1. **Parameter-Bound SHA-256 Digest**: Both bulk writes (`bulk_label_stale_issues`) and single-item writes (`create_issue`, `add_label`, `close_issue`) issue a 16-char `SHA256` token bound to normalized parameters (`repo_name`, `issue_number`, `title`, `body`, `label`, `comment`).
2. **Thread-Safe Single-Use & 5-Minute TTL**: Protected by `_PREVIEW_LOCK`, each token expires after 300 seconds and is atomically consumed on first verification, preventing concurrent double-spend or replay attacks.

---

## 3. XML Delimiter Sandbox & 5-Signature Prompt-Injection Threat Scorer (`tools/triage_issue.py`)

### Vulnerability Pattern
Untrusted GitHub issue titles and bodies can contain indirect prompt-injection payloads attempting to break out of XML tags (`</untrusted_issue_data>`) or coerce the agent into applying malicious labels or closing issues.

### Mitigation
1. **XML Entity Escaping & Length Cap**: Escapes `&`, `<`, `>` and truncates untrusted text to `MAX_UNTRUSTED_CHARS = 4000` before wrapping inside `<untrusted_issue_data>`.
2. **5-Signature Threat Scorer (`detect_prompt_injection_signals`)**: Scans untrusted issue content for instruction overrides, system/role spoofing, XML breakout attempts, tool-execution coercion, and parameter-tampering keywords (`confirmed=True`).
3. **Active Write Gate**: When `apply_labels=True` is requested on an issue where `injection_analysis["detected"]` is `True`, the server automatically aborts the label mutation (`status="injection_blocked"`).

---

## 4. Scoped Session RBAC & Per-Tool Write Velocity Limiter (`policy_engine.py`)

- **Hierarchical RBAC Personas (`active_role` / `MCP_AGENT_ROLE`)**: Enforces 4 least-privilege roles (`read_only`, `triage_operator`, `maintainer`, `admin`) before any write tool executes.
- **60s Sliding-Window Write Velocity Limiter (`max_writes_per_minute`)**: Tracks per-tool write timestamps inside a thread-safe sliding window to halt runaway agent loops before they exhaust GitHub API quota.

---

## 5. Tamper-Evident SHA-256 Hash-Chained Saga Journal & SOC-2 Compliance Exporter

- **Blockchain-Style Audit Log (`transaction_journal.py`)**: Every write record links `prev_hash` and `record_hash = SHA256(canonical_payload || prev_hash)`. Calling `verify_chain_integrity()` or reading `github://journal/integrity` detects any out-of-band log modification or deletion.
- **SOC-2 / ISO-27001 Compliance Attestation (`github://compliance/snapshot`)**: `PolicyEngine.export_compliance_snapshot()` exports a canonical JSON governance snapshot signed with a 64-character `snapshot_sha256` digest.
