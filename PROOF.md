# Cryptographic Engineering Proof & Micro-Benchmark Scorecard (`PROOF.md`)

> **Reproducible in 1 Command**:
> - Windows: `.\venv\Scripts\python.exe eval/generate_proof_report.py`
> - Linux/macOS: `python3 eval/generate_proof_report.py`
> - **Attestation Digest (`proof_sha256`)**: `9928bd4adb9493644975cd7732e46659c96e20d06cc478d5548defa0cc7fab64`
> - **MCP Spec Primitives Verified**: `13 Tools`, `5 Native Resources`, `2 Native Prompts` (`parity_ok = True`)

---

## 1. Intent Routing & Prompt-Injection Benchmark Accuracy

| Benchmark Suite | Queries | Correct | Accuracy | Status |
| :--- | :---: | :---: | :---: | :---: |
| **Standard Natural-Language Benchmark** (`eval/test_queries.json`) | `80` | `80` | **`100.0%`** | `PASS` |
| **Adversarial Prompt-Injection Benchmark** (`eval/adversarial_queries.json`) | `20` | `20` | **`100.0%`** | `PASS` |

---

## 2. Subsystem Micro-Benchmark Latencies (Measured Live via `time.perf_counter_ns`)

| Subsystem Engine | Operation | `p50` Latency ($\mu\text{s}$) | `p95` Latency ($\mu\text{s}$) | Measured Outcome |
| :--- | :--- | :---: | :---: | :--- |
| **Outbound DLP + Shannon Entropy + Luhn PII** | `PolicyEngine.scan_for_secrets()` | `0.8 µs` | `1.0 µs` | Blocks 10 credential patterns, $H(X) \ge 3.8$, Luhn cards |
| **Hybrid RRF + Corpus TF-IDF Vector Engine** | `VectorEngine.hybrid_rrf_search()` (25 docs) | `3369.1 µs` | `3872.2 µs` | Exact error-code + semantic fusion (`0 MB` ML deps) |
| **Context-Window Token Budgeter** | `compact_and_budget_payload()` (10 issues) | `221.4 µs` | `339.5 µs` | `12879 → 1009 tokens` (**`-92.2%`** saved) |
| **SHA-256 Hash-Chained Saga Journal** | `TransactionJournal.verify_chain_integrity()` (10 tx) | `208.7 µs` | `328.1 µs` | `100%` 1-bit tamper detection across chain |
| **Constant-Time HMAC-SHA256 Webhook Guard** | `GitHubWebhookProcessor.verify_signature()` | `3.3 µs` | `3.5 µs` | Timing-attack safe (`hmac.compare_digest`) + nonce guard |
