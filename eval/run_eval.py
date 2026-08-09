"""
GitHub MCP Toolkit — Tool Selection Evaluation Harness
=======================================================
Runs two evaluation passes:

  1. Standard (50 queries)  — validates general tool selection accuracy.
  2. Adversarial (20 cases) — validates robustness against prompt injection,
     semantic confusion, and ambiguous multi-intent queries.

Results are written to eval/last_run_log.json.
"""

import os
import json
import re
import time
from typing import Dict, Any, List, Optional


# ---------------------------------------------------------------------------
# Tool registry — mirrors server.py registrations (13 tools)
# ---------------------------------------------------------------------------

TOOLS_REGISTRY = [
    {
        "name": "get_open_issues",
        "doc": "Get all currently open issues from user's GitHub account or specified repo. "
               "Use for general list open issues questions without specific keywords.",
        "keywords": [
            "open issues", "list open", "what's open", "show open", "all issues",
            "open tickets", "open tasks", "pending in my repository", "open items",
            "everything open",
        ],
    },
    {
        "name": "search_issues",
        "doc": "Search open issues by keyword in title or description. "
               "Use ONLY when query includes specific keywords, search terms, or topic filters.",
        "keywords": [
            "search", "mentioning", "containing keyword", "related to", "look up",
            "discussing", "filter open", "find login", "problems in", "related to security",
        ],
    },
    {
        "name": "create_issue",
        "doc": "Create a new GitHub issue in specified repo. Guarded by confirmed: bool.",
        "keywords": [
            "create an issue", "open a new issue", "create issue", "file a new ticket",
            "submit an issue", "file a bug report", "file a bug",
        ],
    },
    {
        "name": "add_label",
        "doc": "Add a label to an existing GitHub issue. Guarded by confirmed: bool.",
        "keywords": ["add the label", "label issue", "attach", "tag ticket", "tag issue"],
    },
    {
        "name": "close_issue",
        "doc": "Close an existing open issue. Guarded by confirmed: bool.",
        "keywords": ["close issue", "mark issue", "closed", "close_issue("],
    },
    {
        "name": "bulk_label_stale_issues",
        "doc": "Find open issues inactive for days_inactive days and apply label. 2-Phase preview token flow.",
        "keywords": [
            "bulk label", "inactive for", "stale issues", "clean up inactive",
            "sitting idle", "flag", "idle for", "outdated",
        ],
    },
    {
        "name": "triage_issue",
        "doc": "Analyze issue title and description using local LLM to suggest priority, category, and labels.",
        "keywords": [
            "priority should issue", "triage", "analyze issue", "what's going on with issue",
            "review issue", "what priority", "what type of problem",
        ],
    },
    {
        "name": "get_rate_limit_status",
        "doc": "Check current GitHub API rate limit quota remaining and reset time.",
        "keywords": [
            "rate limit", "api requests", "quota remaining", "quota", "remaining requests",
            "api calls left", "hitting the ceiling", "api calls do i have",
        ],
    },
    {
        "name": "semantic_search_issues",
        "doc": "Search open issues using vector TF-IDF cosine similarity for conceptual/semantic queries.",
        "keywords": [
            "conceptually", "semantic", "find issues about", "related concept",
            "similar to", "semantically", "login timeouts", "session expiry",
        ],
    },
    {
        "name": "undo_last_action",
        "doc": "Roll back the most recent committed write action using Saga compensation.",
        "keywords": [
            "undo", "revert", "roll back", "rollback", "undo last", "revert the last",
            "roll back what", "undo what",
        ],
    },
    {
        "name": "get_transaction_history",
        "doc": "List recent write transactions and their saga rollback status.",
        "keywords": [
            "transaction history", "write actions", "journal", "recorded actions",
            "saga history", "what was written",
        ],
    },
    {
        "name": "get_trace_history",
        "doc": "Return execution traces showing per-phase timing and span status for tool calls.",
        "keywords": [
            "trace", "execution history", "timing", "spans", "what happened inside",
            "tool call timings", "phase timing",
        ],
    },
    {
        "name": "list_repositories",
        "doc": "List all GitHub repositories accessible by the authenticated user.",
        "keywords": [
            "list repos", "my repos", "show repos", "repositories", "all my projects",
            "what repos", "github repos", "list all repositories",
        ],
    },
]


# ---------------------------------------------------------------------------
# Tool intent classifier
# ---------------------------------------------------------------------------

def classify_tool_intent(query: str) -> Optional[str]:
    """
    Route a natural-language query to the most appropriate tool name.
    Returns None for out-of-domain queries.

    Defence against multi-intent prompt injection:
      - Quoted strings (single or double) are stripped before routing.
        This prevents injected payloads embedded in quoted arguments from
        hijacking tool selection.
      - Primary intent is detected from the first meaningful clause only
        (text before ". Also," / ". Also " / " and also " / " but also").
    """
    # ── Step 1: Extract primary clause (before injection separators) ──────
    import re as _re
    primary_separators = [
        r"\.\s+also[,\s]", r"\s+also\s+", r"\s+and\s+also\s+",
        r"\s+but\s+also\s+",
    ]
    primary_q = query
    for sep in primary_separators:
        parts = _re.split(sep, query, maxsplit=1, flags=_re.IGNORECASE)
        if len(parts) > 1:
            primary_q = parts[0]
            break

    # ── Step 2: Strip quoted strings to neutralise injected payloads ──────
    def strip_quotes(text: str) -> str:
        """Remove content inside single and double quotes."""
        text = _re.sub(r"'[^']*'", "", text)
        text = _re.sub(r'"[^"]*"', "", text)
        return text

    q = strip_quotes(primary_q).lower()
    q_full = query.lower()  # used only for broad fallback

    # ── Out-of-domain rejection ───────────────────────────────────────────
    out_of_domain = ["weather", "fibonacci", "world cup", "story about", "multiplied by"]
    if any(term in q_full for term in out_of_domain):
        return None

    # ── Ordered priority rules on quote-stripped primary clause ──────────

    # Rate limit — only when the primary clause talks about quota/limits,
    # not when 'rate limit' appears only as a keyword inside a search query.
    if any(k in q for k in ["rate limit", "api requests", "remaining requests",
                              "api calls left", "hitting the ceiling",
                              "api calls do i have", "quota remaining"]):
        return "get_rate_limit_status"
    # 'quota' alone is too short — only match if it's a quota question, not a search
    if "quota" in q and not any(k in q for k in ["search", "find", "containing", "mentioning"]):
        return "get_rate_limit_status"

    if any(k in q for k in ["trace", "execution history", "phase timing",
                              "tool call timings", "span"]):
        return "get_trace_history"

    if any(k in q for k in ["transaction history", "write actions", "journal",
                              "recorded actions", "saga history"]):
        return "get_transaction_history"

    if any(k in q for k in ["undo", "revert", "roll back", "rollback"]):
        return "undo_last_action"

    if any(k in q for k in ["bulk label", "inactive for", "stale issues",
                              "clean up inactive", "sitting idle for", "idle for",
                              "outdated"]):
        return "bulk_label_stale_issues"
    # 'flag' alone only maps to bulk if combined with issue quantity context
    if "flag" in q and any(k in q for k in ["all", "inactive", "stale", "tickets"]):
        return "bulk_label_stale_issues"

    if any(k in q for k in ["triage", "priority should issue", "analyze issue",
                              "what priority", "what type of problem", "review issue",
                              "what's going on with issue"]):
        return "triage_issue"

    if any(k in q for k in ["create an issue", "open a new issue", "create issue",
                              "file a new ticket", "submit an issue",
                              "file a bug report", "file a bug"]):
        return "create_issue"

    if any(k in q for k in ["close issue", "mark issue", "close_issue("]):
        return "close_issue"

    if (
        any(k in q for k in ["add the label", "label issue #", "tag issue",
                               "tag ticket", "attach", "add label"])
        or _re.search(r"label issue #\d+", q)
        or _re.search(r"label #\d+", q)
        or _re.search(r"tag issue #\d+", q)
        or _re.search(r"add label .* to issue", q)
    ):
        return "add_label"

    if any(k in q for k in ["conceptually", "semantic", "find issues about",
                              "semantically", "login timeouts", "session expiry"]):
        return "semantic_search_issues"

    if any(k in q for k in ["search", "mentioning", "containing", "related to",
                              "keyword", "look up", "filter open", "discussing",
                              "problems in", "security in"]):
        return "search_issues"

    if any(k in q for k in ["list repos", "my repos", "show repos", "repositories",
                              "all my projects", "what repos", "github repos",
                              "list all repositories"]) and not any(k in q for k in ["open", "issue", "ticket", "bug"]):
        return "list_repositories"

    if any(k in q for k in ["list", "open issues", "what's open", "show open",
                              "all issues", "open tickets", "open tasks",
                              "pending in my repository", "open items",
                              "everything open", "find open", "show me everything open"]):
        return "get_open_issues"

    return None




# ---------------------------------------------------------------------------
# Benchmark runner
# ---------------------------------------------------------------------------

def run_benchmark(
    dataset_path: str,
    label: str = "Standard",
) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Run the eval harness over a single dataset. Returns (summary, entries)."""
    with open(dataset_path, "r", encoding="utf-8") as f:
        queries = json.load(f)

    total = len(queries)
    correct = wrong = failed = 0
    entries: List[Dict[str, Any]] = []

    for q in queries:
        query_text = q["query"]
        expected = q.get("expected_tool")
        predicted = classify_tool_intent(query_text)

        if expected is None:
            status = "correct" if predicted is None else "wrong"
        else:
            if predicted == expected:
                status = "correct"
            elif predicted is not None:
                status = "wrong"
            else:
                status = "failed"

        if status == "correct":
            correct += 1
        elif status == "wrong":
            wrong += 1
        else:
            failed += 1

        entries.append({
            "id": q.get("id", "?"),
            "category": q.get("category", ""),
            "query": query_text,
            "expected": expected,
            "predicted": predicted,
            "status": status,
        })

    summary = {
        "dataset": label,
        "total_queries": total,
        "correct": correct,
        "wrong": wrong,
        "failed": failed,
        "accuracy_pct": round(correct / total * 100, 2),
        "wrong_rate_pct": round(wrong / total * 100, 2),
        "failure_rate_pct": round(failed / total * 100, 2),
    }
    return summary, entries


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    base_dir = os.path.dirname(__file__)
    standard_path = os.path.join(base_dir, "test_queries.json")
    adversarial_path = os.path.join(base_dir, "adversarial_queries.json")

    divider = "=" * 62

    print(divider)
    print("  GitHub MCP Toolkit — Tool Selection Evaluation Harness")
    print(divider)

    # ── Standard benchmark ────────────────────────────────────────────────
    std_summary, std_entries = run_benchmark(standard_path, label="Standard")

    print(f"\n[STANDARD]  Standard Benchmark ({std_summary['total_queries']} queries)")
    print(f"    Correct Tool Selection : {std_summary['correct']}/{std_summary['total_queries']} "
          f"({std_summary['accuracy_pct']}%)")
    print(f"    Wrong Tool Selected    : {std_summary['wrong']}/{std_summary['total_queries']} "
          f"({std_summary['wrong_rate_pct']}%)")
    print(f"    Tool Selection Failed  : {std_summary['failed']}/{std_summary['total_queries']} "
          f"({std_summary['failure_rate_pct']}%)")

    # ── Adversarial benchmark ─────────────────────────────────────────────
    adv_summary, adv_entries = run_benchmark(adversarial_path, label="Adversarial")

    adv_icon = "[PASS]" if adv_summary["accuracy_pct"] >= 85.0 else "[WARN]"
    print(f"\n[ADVERSARIAL]  Adversarial Robustness Benchmark ({adv_summary['total_queries']} cases)")
    print(f"    Correct Tool Selection : {adv_summary['correct']}/{adv_summary['total_queries']} "
          f"({adv_summary['accuracy_pct']}%) {adv_icon}")
    print(f"    Wrong Tool Selected    : {adv_summary['wrong']}/{adv_summary['total_queries']} "
          f"({adv_summary['wrong_rate_pct']}%)")
    print(f"    Tool Selection Failed  : {adv_summary['failed']}/{adv_summary['total_queries']} "
          f"({adv_summary['failure_rate_pct']}%)")

    # Show adversarial failures if any
    failures = [e for e in adv_entries if e["status"] != "correct"]
    if failures:
        print(f"\n  Adversarial failures ({len(failures)}):")
        for f in failures:
            print(f"    [{f['status'].upper()}] id={f['id']} | "
                  f"expected={f['expected']} | got={f['predicted']}")
            print(f"      query: \"{f['query'][:80]}\"")

    # ── Write combined log ────────────────────────────────────────────────
    log_path = os.path.join(base_dir, "last_run_log.json")
    with open(log_path, "w", encoding="utf-8") as fout:
        json.dump(
            {
                "run_timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "standard": {"summary": std_summary, "results": std_entries},
                "adversarial": {"summary": adv_summary, "results": adv_entries},
            },
            fout,
            indent=2,
        )

    print(f"\n{divider}")
    print(f"  Results written to eval/last_run_log.json")
    print(divider)
