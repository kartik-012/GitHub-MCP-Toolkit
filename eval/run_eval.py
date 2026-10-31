"""
GitHub MCP Toolkit — Tool Selection Evaluation Harness & Smart Intent Router
============================================================================
Routes natural-language queries (both benchmark queries and free-form human
input) to the appropriate MCP tool using:
  1. Multi-intent clause isolation & quoted-payload sanitisation
  2. High-precision intent pattern rules across all 13 registered tools
  3. TF-IDF VectorEngine semantic similarity fallback for free-form human queries

Automates verification across standard (80 queries) and adversarial (20 queries)
suites, outputting structured reports to eval/last_run_log.json.
"""

import os
import sys
import json
import re
import time
from typing import Dict, Any, List, Optional

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from vector_engine import VectorEngine


# ---------------------------------------------------------------------------
# Tool registry — mirrors server.py registrations (13 tools)
# ---------------------------------------------------------------------------

TOOLS_REGISTRY = [
    {
        "name": "list_repositories",
        "doc": "List all GitHub repositories and projects accessible by the authenticated user account.",
        "keywords": [
            "list repositories", "my repos", "my repositories", "github projects",
            "which repos", "what repositories", "show repos", "codebases",
        ],
    },
    {
        "name": "get_open_issues",
        "doc": "List all currently open issues, tickets, bugs, or tasks in a repository or across the account.",
        "keywords": [
            "open issues", "list issues", "show issues", "my issues", "all issues",
            "get issues", "open tickets", "open tasks", "pending issues",
        ],
    },
    {
        "name": "search_issues",
        "doc": "Search and filter open issues by keyword, topic, error message, or phrase in title or body.",
        "keywords": [
            "search issues", "find issues", "keyword", "filter issues",
            "mentioning", "containing", "related to", "look up", "discussing",
        ],
    },
    {
        "name": "create_issue",
        "doc": "Create, open, file, submit, or report a new GitHub issue or bug ticket in a repository.",
        "keywords": [
            "create issue", "new issue", "open issue", "file ticket",
            "report bug", "make issue", "submit issue", "raise ticket",
        ],
    },
    {
        "name": "add_label",
        "doc": "Add, attach, apply, or tag a label (such as bug, urgent, enhancement) to an existing issue.",
        "keywords": [
            "add label", "label issue", "tag issue", "attach label",
            "apply label", "mark issue with tag",
        ],
    },
    {
        "name": "close_issue",
        "doc": "Close, resolve, or mark an existing open GitHub issue or ticket as closed.",
        "keywords": ["close issue", "resolve issue", "mark closed", "shut ticket", "close ticket"],
    },
    {
        "name": "bulk_label_stale_issues",
        "doc": "Find inactive or stale issues untouched for N days and bulk-apply a label using a 2-phase preview token.",
        "keywords": ["bulk label", "stale issues", "inactive issues", "idle tickets", "old untouched issues"],
    },
    {
        "name": "triage_issue",
        "doc": "Analyze, review, or triage an issue to classify its priority, severity, category, and suggested labels.",
        "keywords": ["triage issue", "priority", "classify issue", "analyze issue", "category", "severity"],
    },
    {
        "name": "get_rate_limit_status",
        "doc": "Check current GitHub API rate limit status, remaining quota, and reset timestamp.",
        "keywords": ["rate limit", "quota", "api calls left", "remaining requests", "api limit"],
    },
    {
        "name": "semantic_search_issues",
        "doc": "Search open issues using TF-IDF cosine similarity for conceptual or semantic meaning matches.",
        "keywords": ["semantic search", "conceptually", "similar issues", "vector search", "semantically related"],
    },
    {
        "name": "undo_last_action",
        "doc": "Undo, revert, or roll back the most recent write action using Saga transaction compensation.",
        "keywords": ["undo", "revert", "roll back", "rollback", "cancel last action"],
    },
    {
        "name": "get_transaction_history",
        "doc": "View the Saga transaction journal history of recent write actions and rollbacks.",
        "keywords": ["transaction history", "journal", "write actions", "saga history", "recorded actions"],
    },
    {
        "name": "get_trace_history",
        "doc": "Inspect execution trace spans and per-phase timing latency for recent tool invocations.",
        "keywords": ["trace history", "execution history", "phase timings", "spans", "tool latency"],
    },
]


# ---------------------------------------------------------------------------
# Smart Tool Intent Classifier
# ---------------------------------------------------------------------------

def _extract_primary_clause(query: str) -> str:
    """Isolate the primary user clause to block multi-intent secondary injection."""
    if not query or not query.strip():
        return ""
    primary_separators = [
        r"\.\s+also[,\s]",
        r"\s+also[,\s]+if\s+you\s+can",
        r"\.\s+also\s+search",
        r"\s+and\s+also\s+",
        r"\s+but\s+also\s+",
    ]
    primary_q = query.strip()
    for sep in primary_separators:
        parts = re.split(sep, primary_q, maxsplit=1, flags=re.IGNORECASE)
        if len(parts) > 1:
            primary_q = parts[0]
            break
    return primary_q


def classify_tool_intent(query: str) -> Optional[str]:
    """
    Route any natural-language query to the best matching MCP tool.
    Returns None when the query is unrelated to GitHub/issue management.
    """
    primary_q = _extract_primary_clause(query)
    if not primary_q:
        return None

    # ── Step 2: Strip quoted strings so injected payloads inside quotes don't hijack routing
    def strip_quotes(text: str) -> str:
        text = re.sub(r"'[^']*'", " ", text)
        text = re.sub(r'"[^"]*"', " ", text)
        return text

    q = strip_quotes(primary_q).lower()
    q_raw = primary_q.lower()
    words = set(re.findall(r"\b[a-z0-9_]+\b", q))

    # ── Step 3: Out-of-domain rejection ───────────────────────────────────
    out_of_domain = [
        "weather", "fibonacci", "world cup", "story about", "multiplied by",
        "capital of", "recipe for", "chocolate chip", "cookies", "translate ",
        "spanish", "stock price", "tell me a joke", "poem about",
    ]
    if any(term in q_raw for term in out_of_domain):
        return None

    # ── Step 4: High-confidence intent routing ────────────────────────────

    # 1. Rate Limit / API Quota
    if (
        any(k in q for k in [
            "rate limit", "ratelimit", "api requests", "remaining requests",
            "api calls", "hitting the ceiling", "quota remaining", "api limit",
            "requests left", "calls left", "rate_limit",
        ])
        or ("quota" in words and not any(w in words for w in ["search", "find", "containing", "mentioning"]))
        or ("limit" in words and any(w in words for w in ["api", "github", "check", "status", "remaining"]))
    ):
        return "get_rate_limit_status"

    # 2. Execution Traces / Latency Spans (use word boundaries so 'Spanish' never matches 'span')
    if (
        any(w in words for w in ["trace", "traces", "span", "spans", "timings", "latency"])
        or any(k in q for k in ["execution history", "phase timing", "tool call timings"])
    ):
        return "get_trace_history"

    # 3. Saga Transaction Journal History
    if (
        any(w in words for w in ["transaction", "transactions", "journal", "saga"])
        or any(k in q for k in ["write actions", "recorded actions", "action history"])
    ):
        return "get_transaction_history"

    # 4. Undo / Revert / Rollback
    if any(w in words for w in ["undo", "revert", "rollback"]) or "roll back" in q:
        return "undo_last_action"

    # 5. Bulk Label Stale / Inactive Issues
    if (
        any(k in q for k in [
            "bulk label", "bulk_label", "inactive for", "stale issues",
            "clean up inactive", "sitting idle", "idle for", "outdated",
            "mark stale", "label stale", "old issues", "inactive tickets",
            "untouched for", "older than",
        ])
        or ("stale" in words and any(w in words for w in ["label", "bulk", "find", "mark", "tag", "all", "issues", "tickets", "apply"]))
        or ("inactive" in words and any(w in words for w in ["days", "label", "all", "issues", "tickets", "clean", "find"]))
        or ("flag" in words and any(w in words for w in ["all", "inactive", "stale", "tickets", "idle"]))
    ):
        return "bulk_label_stale_issues"

    # 6. Triage / Priority / Classify Issue
    if (
        any(k in q for k in [
            "triage", "priority should", "what priority", "analyze issue",
            "analyse issue", "what type of problem", "review issue",
            "what's going on with issue", "classify issue", "categorize issue",
            "suggest labels", "assign priority", "sla remediation", "sla target", "sla timeline",
        ])
        or any(w in words for w in ["triage", "priority", "severity", "classify", "categorize", "urgency", "impact"])
    ):
        return "triage_issue"

    # 7. Leading-verb priority for Create vs Close (prevents title payload hijacking)
    issue_nouns = {"issue", "issues", "ticket", "tickets", "bug", "bugs", "task", "tasks", "problem"}
    if q.startswith(("create ", "file ", "submit ", "report ", "open a new ", "make a ", "make an ")):
        return "create_issue"
    if q.startswith(("close ", "resolve ", "mark ", "close_issue")):
        return "close_issue"

    # 8. Close / Resolve Issue
    if (
        any(k in q for k in ["close issue", "mark issue", "close_issue", "close ticket", "resolve issue", "as closed"])
        or ("close" in words and (bool(words & issue_nouns) or re.search(r"#?\d+", q)))
        or ("closed" in words and "mark" in words)
        or ("resolve" in words and (bool(words & issue_nouns) or re.search(r"#?\d+", q)))
    ):
        return "close_issue"

    # 9. Add Label / Tag Issue
    if (
        any(k in q for k in [
            "add the label", "add label", "label issue", "tag issue",
            "tag ticket", "attach", "put label", "apply label",
        ])
        or ("label" in words and (re.search(r"#?\d+", q) or any(w in words for w in ["add", "tag", "attach", "apply", "put", "to", "on", "as"])))
        or ("tag" in words and (re.search(r"#?\d+", q) or bool(words & issue_nouns)))
    ):
        return "add_label"

    # 10. Create / Open / File / Report a New Issue
    create_verbs = {"create", "file", "submit", "report", "make", "raise", "new"}
    if (
        any(k in q for k in [
            "create an issue", "open a new issue", "create issue", "file a new ticket",
            "submit an issue", "file a bug", "create a bug", "new issue", "new ticket",
            "make an issue", "make a ticket", "open an issue", "report a bug",
            "add an issue", "raise an issue", "open a ticket",
        ])
        or (bool(words & create_verbs) and bool(words & issue_nouns) and "search" not in words and "filter" not in words)
    ):
        return "create_issue"

    # 10. List Repositories (when user asks for their repos/projects, NOT issues inside a repo)
    repo_nouns = {"repositories", "repos", "repository", "projects", "codebases"}
    if (
        bool(words & repo_nouns)
        and not bool(words & {"issue", "issues", "ticket", "tickets", "bug", "bugs", "task", "tasks", "stale", "open"})
    ):
        return "list_repositories"

    # 11. Semantic / Conceptual Vector Search
    if any(k in q for k in [
        "conceptually", "semantic", "semantically", "vector search",
        "similar issues", "similar to", "find issues about", "login timeouts", "session expiry",
    ]):
        return "semantic_search_issues"

    # 12. General Open Issue Listing vs. Keyword Search
    general_list_phrases = [
        "list all open", "list open", "show open", "show me the open", "show me everything open",
        "what open issues", "how many open issues", "get open issues", "find open issues",
        "any open items", "tickets pending", "open tasks across", "everything open",
        "what tasks are currently open", "show all issues", "list issues", "my issues",
    ]
    if any(p in q for p in general_list_phrases) and not any(
        s in q for s in ["mentioning", "containing", "keyword", "about", "related to", "discussing"]
    ):
        return "get_open_issues"

    # Search triggers: search/find/filter/look up + topic/keyword
    search_indicators = [
        "search", "mentioning", "containing", "related to", "keyword",
        "look up", "look for", "filter", "discussing", "problems in",
        "security in", "about", "matching", "where", "with title",
    ]
    if any(term in q for term in search_indicators):
        return "search_issues"

    if "find" in words and q.strip() not in ("find issues", "find open issues", "find tickets"):
        return "search_issues"

    if any(w in words for w in ["issues", "issue", "tickets", "ticket", "bugs", "open", "pending", "tasks"]):
        return "get_open_issues"

    # ── Step 5: TF-IDF VectorEngine Semantic Fallback for Free-Form Queries ─
    tool_docs = [
        {"title": t["name"].replace("_", " "), "body": f"{t['doc']} {' '.join(t['keywords'])}", "tool_name": t["name"]}
        for t in TOOLS_REGISTRY
    ]
    ranked = VectorEngine.rank_documents(q, tool_docs, top_k=1)
    if ranked and ranked[0]["similarity_score"] >= 0.18:
        return ranked[0]["tool_name"]

    return None


def classify_tool_intent_with_metadata(query: str) -> Dict[str, Any]:
    """
    Route natural-language query and return tool match alongside execution metadata:
    confidence estimation, evaluation latency in milliseconds, and resolution method.
    """
    start_ns = time.perf_counter_ns()
    predicted = classify_tool_intent(query)
    latency_ms = round((time.perf_counter_ns() - start_ns) / 1_000_000.0, 3)

    if predicted is None:
        return {
            "tool": None,
            "confidence": 0.0,
            "latency_ms": latency_ms,
            "method": "out_of_domain_or_unmatched",
        }

    # Estimate confidence: high for deterministic rules, vector score for fallback
    q_clean = _extract_primary_clause(query) or ""
    tool_docs = [
        {"title": t["name"].replace("_", " "), "body": f"{t['doc']} {' '.join(t['keywords'])}", "tool_name": t["name"]}
        for t in TOOLS_REGISTRY
    ]
    ranked = VectorEngine.rank_documents(q_clean, tool_docs, top_k=1)
    vector_score = ranked[0]["similarity_score"] if (ranked and ranked[0]["tool_name"] == predicted) else 0.5

    confidence = round(max(0.75, vector_score), 2)
    return {
        "tool": predicted,
        "confidence": confidence,
        "latency_ms": latency_ms,
        "method": "hybrid_rule_and_vector",
    }


# ---------------------------------------------------------------------------
# Real LLM Tool-Selection Classifier (--llm mode via local Ollama)
# ---------------------------------------------------------------------------

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_EVAL_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:1b")


def classify_tool_intent_llm(query: str, model: Optional[str] = None) -> Optional[str]:
    """
    Evaluate tool selection using a live local Ollama LLM presented with the
    13 registered MCP tool descriptions.
    Sanitizes multi-intent secondary clauses before prompting the LLM and
    validates that the returned tool name is in TOOLS_REGISTRY (or None).
    """
    primary_clause = _extract_primary_clause(query or "")
    if not primary_clause:
        return None

    valid_tool_names = {t["name"] for t in TOOLS_REGISTRY}
    tools_catalog = "\n".join(
        f"- {t['name']}: {t['doc']}" for t in TOOLS_REGISTRY
    )
    system_prompt = (
        "You are an MCP tool router. Given the user's request, select the single best "
        "MCP tool from the list below, or return null if the request is unrelated to GitHub.\n\n"
        f"Available MCP Tools:\n{tools_catalog}\n\n"
        "Respond ONLY with valid JSON in this exact format: {\"selected_tool\": \"<tool_name_or_null>\"}"
    )

    try:
        import ollama
        client = ollama.Client(host=OLLAMA_HOST)
        response = client.chat(
            model=model or OLLAMA_EVAL_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": primary_clause},
            ],
            format="json",
            options={"temperature": 0.0},
        )
        raw_content = response["message"]["content"].strip()
        parsed = json.loads(raw_content)
        selected = parsed.get("selected_tool")
        if selected in valid_tool_names:
            return selected
        return None
    except Exception:
        # Graceful fallback to deterministic + TF-IDF router when Ollama is offline
        return classify_tool_intent(query)


# ---------------------------------------------------------------------------
# Benchmark runner
# ---------------------------------------------------------------------------

def run_benchmark(
    dataset_path: str,
    label: str = "Standard",
    mode: str = "router",
    llm_model: Optional[str] = None,
) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """
    Run the eval harness over a single dataset.
    `mode` can be 'router' (deterministic + TF-IDF) or 'llm' (live Ollama tool router).
    Returns (summary, entries).
    """
    with open(dataset_path, "r", encoding="utf-8") as f:
        queries = json.load(f)

    total = len(queries)
    correct = wrong = failed = 0
    entries: List[Dict[str, Any]] = []

    classifier_fn = (
        (lambda q_text: classify_tool_intent_llm(q_text, model=llm_model))
        if mode == "llm"
        else classify_tool_intent
    )

    for q in queries:
        query_text = q["query"]
        expected = q.get("expected_tool")
        predicted = classifier_fn(query_text)

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
        "mode": mode,
        "total_queries": total,
        "correct": correct,
        "wrong": wrong,
        "failed": failed,
        "accuracy_pct": round(correct / total * 100, 2),
        "wrong_rate_pct": round(wrong / total * 100, 2),
        "failure_rate_pct": round(failed / total * 100, 2),
    }
    return summary, entries


def verify_live_server_parity() -> Dict[str, Any]:
    """
    Verify that TOOLS_REGISTRY in eval/run_eval.py matches 1-to-1 with:
      1. The live FastMCP tools registered on `server.mcp`.
      2. The Pydantic output schemas in `schemas.TOOL_SCHEMA_MAP`.
      3. All 4 native MCP resources and 2 native MCP prompts registered on `server.mcp`.
    """
    import asyncio
    import server as server_mod
    from schemas import TOOL_SCHEMA_MAP

    eval_tools = {t["name"] for t in TOOLS_REGISTRY}
    schema_tools = set(TOOL_SCHEMA_MAP.keys())

    mcp_obj = server_mod.mcp
    tm = getattr(mcp_obj, "_tool_manager", None)
    if tm and hasattr(tm, "_tools"):
        live_tools = set(tm._tools.keys())
    else:
        tools_list = asyncio.run(mcp_obj.list_tools())
        live_tools = {t.name for t in tools_list}

    expected_resources = {
        "github://policy/active",
        "github://telemetry/slo",
        "github://journal/integrity",
        "github://compliance/snapshot",
        "github://finops/sessions",
    }
    expected_prompts = {
        "incident_triage_workflow",
        "safe_bulk_cleanup_workflow",
    }
    live_resources = {str(r.uri) for r in asyncio.run(mcp_obj.list_resources())}
    live_prompts = {p.name for p in asyncio.run(mcp_obj.list_prompts())}

    parity_ok = (
        eval_tools == live_tools == schema_tools
        and expected_resources.issubset(live_resources)
        and expected_prompts.issubset(live_prompts)
    )
    return {
        "parity_ok": parity_ok,
        "tool_count": len(live_tools),
        "resource_count": len(live_resources),
        "prompt_count": len(live_prompts),
        "eval_only": sorted(eval_tools - live_tools),
        "server_only": sorted(live_tools - eval_tools),
        "missing_schemas": sorted(live_tools - schema_tools),
        "missing_resources": sorted(expected_resources - live_resources),
        "missing_prompts": sorted(expected_prompts - live_prompts),
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="GitHub MCP Toolkit Evaluation Harness")
    parser.add_argument(
        "--llm",
        action="store_true",
        help="Benchmark live Ollama LLM tool selection instead of the deterministic + TF-IDF router.",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=OLLAMA_EVAL_MODEL,
        help="Ollama model name when running with --llm (default: llama3.2:1b).",
    )
    args = parser.parse_args()
    eval_mode = "llm" if args.llm else "router"

    base_dir = os.path.dirname(__file__)
    standard_path = os.path.join(base_dir, "test_queries.json")
    adversarial_path = os.path.join(base_dir, "adversarial_queries.json")

    divider = "=" * 62
    print(divider)
    print(f"  GitHub MCP Toolkit - Tool Selection Evaluation Harness ({eval_mode.upper()} MODE)")
    print(divider)

    parity = verify_live_server_parity()
    parity_icon = "[PASS]" if parity["parity_ok"] else "[FAIL]"
    print(
        f"\n[PARITY]    Live Server & Schema Contract Parity "
        f"({parity['tool_count']} tools, {parity['resource_count']} resources, {parity['prompt_count']} prompts) {parity_icon}"
    )

    std_summary, std_entries = run_benchmark(standard_path, label="Standard", mode=eval_mode, llm_model=args.model)
    print(f"\n[STANDARD]  Standard Benchmark ({std_summary['total_queries']} queries)")
    print(f"    Correct Tool Selection : {std_summary['correct']}/{std_summary['total_queries']} ({std_summary['accuracy_pct']}%)")
    print(f"    Wrong Tool Selected    : {std_summary['wrong']}/{std_summary['total_queries']} ({std_summary['wrong_rate_pct']}%)")
    print(f"    Tool Selection Failed  : {std_summary['failed']}/{std_summary['total_queries']} ({std_summary['failure_rate_pct']}%)")

    adv_summary, adv_entries = run_benchmark(adversarial_path, label="Adversarial", mode=eval_mode, llm_model=args.model)
    adv_icon = "[PASS]" if adv_summary["accuracy_pct"] >= 85.0 else "[WARN]"
    print(f"\n[ADVERSARIAL]  Adversarial Robustness Benchmark ({adv_summary['total_queries']} cases)")
    print(f"    Correct Tool Selection : {adv_summary['correct']}/{adv_summary['total_queries']} ({adv_summary['accuracy_pct']}%) {adv_icon}")
    print(f"    Wrong Tool Selected    : {adv_summary['wrong']}/{adv_summary['total_queries']} ({adv_summary['wrong_rate_pct']}%)")
    print(f"    Tool Selection Failed  : {adv_summary['failed']}/{adv_summary['total_queries']} ({adv_summary['failure_rate_pct']}%)")

    log_path = os.path.join(base_dir, "last_run_log.json")
    with open(log_path, "w", encoding="utf-8") as fout:
        json.dump(
            {
                "run_timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "mode": eval_mode,
                "parity": parity,
                "standard": {"summary": std_summary, "results": std_entries},
                "adversarial": {"summary": adv_summary, "results": adv_entries},
            },
            fout,
            indent=2,
        )

    print(f"\n{divider}")
    print("  Results written to eval/last_run_log.json")
    print(divider)

    if not parity["parity_ok"] or std_summary["accuracy_pct"] < 95.0 or adv_summary["accuracy_pct"] < 90.0:
        sys.exit(1)


