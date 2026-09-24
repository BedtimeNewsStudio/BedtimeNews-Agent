"""
Evaluation tool for pure retrieval (no agentic processing).

This tool exercises the raw vector database retrieval functionality using
queries from eval_queries.py. It bypasses the agentic RAG pipeline and directly
calls the retriever for evaluation purposes. It is a manual evaluation harness,
not an automated test.

Usage:
    # Score the labelled set and append the run to eval_results/retriever.json
# (repo) or /var/log/agent/eval_results/retriever.json (container)
    docker compose run --rm --build \
      --volume ./agent/eval_results:/app/eval_results \
      agent python -m src.eval_retriever --labelled

    # Evaluate a single query directly
    docker compose exec agent python -m src.eval_retriever --query "你的问题"
    docker compose exec agent python -m src.eval_retriever -q "你的问题"

    # List available categories
    docker compose exec agent python -m src.eval_retriever --list-categories

    # Evaluate all queries
    docker compose exec agent python -m src.eval_retriever

    # Evaluate specific category
    docker compose exec agent python -m src.eval_retriever --category education

    # Random sample of queries
    docker compose exec agent python -m src.eval_retriever --random 10

    # Adjust retrieval parameters
    docker compose exec agent python -m src.eval_retriever --match-count 10 --threshold 0.6

"""

import argparse
import json
import random
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .eval_queries import (
    ALL_QUERIES,
    CATEGORY_NAMES_CN,
    FLAT_QUERIES,
)
from .models import RetrieveRequest
from .retriever import retriever
from .settings import settings

LABELLED_QUERIES_FILE = Path(__file__).with_name("eval_retriever_labels.json")


def _default_results_file() -> Path:
    """Local runs write into the repo; the container falls back to the log volume.

    /app is read-only for the unprivileged container user, so the repo-relative
    path is only used when it can actually be created.
    """
    repo_path = Path(__file__).parents[1] / "eval_results" / "retriever.json"
    try:
        repo_path.parent.mkdir(parents=True, exist_ok=True)
        return repo_path
    except OSError:
        return Path("/var/log/agent/eval_results/retriever.json")


DEFAULT_RESULTS_FILE = _default_results_file()


def main():
    parser = argparse.ArgumentParser(
        description="Test BedtimeNews retrieval without agentic processing",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    query_group = parser.add_mutually_exclusive_group()
    query_group.add_argument(
        "--query",
        "-q",
        type=str,
        help="Test a single query directly from command line",
    )
    query_group.add_argument(
        "--category",
        type=str,
        choices=list(ALL_QUERIES.keys()),
        help="Test queries from a specific category",
    )
    query_group.add_argument(
        "--random",
        type=int,
        metavar="N",
        help="Test N random queries",
    )
    query_group.add_argument(
        "--labelled",
        action="store_true",
        help="Score the labelled query set and append the run to JSON",
    )

    parser.add_argument(
        "--match-count",
        type=int,
        default=settings.retrieval_top_k,
        help="Number of results to retrieve per query (default: retrieval_top_k)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=settings.match_threshold,
        help="Minimum similarity threshold (default: match_threshold)",
    )

    parser.add_argument(
        "--list-categories",
        action="store_true",
        help="List all available query categories and exit",
    )
    parser.add_argument(
        "--results-file",
        type=Path,
        default=DEFAULT_RESULTS_FILE,
        help=(
            f"JSON history to append labelled runs to (default: {DEFAULT_RESULTS_FILE})"
        ),
    )
    parser.add_argument(
        "--run-label",
        help="Optional label stored with a labelled run (for example, a commit SHA)",
    )

    args = parser.parse_args()

    if args.list_categories:
        print("\nAvailable query categories:")
        print("=" * 80)
        for category, queries in ALL_QUERIES.items():
            cn_name = CATEGORY_NAMES_CN.get(category, "")
            print(f"  {category:25s}: {len(queries):3d} queries  # {cn_name}")
        print("=" * 80)
        print(f"\nTotal: {len(ALL_QUERIES)} categories, {len(FLAT_QUERIES)} queries")
        return

    if args.labelled:
        queries = _load_labelled_queries()
        print(f"\nSelected labelled set ({len(queries)} queries)")
    elif args.query:
        queries = [{"category": "custom", "query": args.query}]
        print(f"\nTesting custom query: {args.query}")
    elif args.category:
        queries = [
            {"category": args.category, "query": q}
            for q in ALL_QUERIES.get(args.category, [])
        ]
        print(f"\nSelected category: {args.category} ({len(queries)} queries)")
    elif args.random:
        queries = random.sample(FLAT_QUERIES, min(args.random, len(FLAT_QUERIES)))
        print(f"\nSelected {len(queries)} random queries")
    else:
        queries = FLAT_QUERIES
        print(f"\nTesting all queries ({len(queries)} total)")

    if not queries:
        print("No queries to test", file=sys.stderr)
        sys.exit(1)

    try:
        run_result = _run_retrieval_test(
            queries=queries,
            match_count=args.match_count,
            match_threshold=args.threshold,
        )
        if args.labelled:
            run_result["run_label"] = args.run_label
            _append_run_result(args.results_file, run_result)
            print(f"\nAppended labelled result to {args.results_file}")
    except KeyboardInterrupt:
        print("\n\nTest interrupted by user", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"\nTest failed: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        sys.exit(1)


def _load_labelled_queries() -> list[dict[str, str]]:
    """Load and validate the fixed, human-labelled retrieval set."""
    data = json.loads(LABELLED_QUERIES_FILE.read_text(encoding="utf-8"))
    queries = data.get("queries")
    if not isinstance(queries, list) or not queries:
        raise ValueError(f"No labelled queries found in {LABELLED_QUERIES_FILE}")

    known_queries = {item["query"] for item in FLAT_QUERIES}
    required_fields = {"category", "query", "doc_id"}
    for index, item in enumerate(queries):
        if not isinstance(item, dict) or not required_fields <= item.keys():
            raise ValueError(
                f"Label {index} must contain: {', '.join(sorted(required_fields))}"
            )
        if item["query"] not in known_queries:
            raise ValueError(
                f"Labelled query is not present in eval_queries.py: {item['query']}"
            )

    return queries


def _append_run_result(results_file: Path, run_result: dict[str, Any]) -> None:
    """Append a run while keeping the tracked results file valid JSON."""
    if results_file.exists():
        history = json.loads(results_file.read_text(encoding="utf-8"))
    else:
        history = {"schema_version": 1, "runs": []}

    if history.get("schema_version") != 1 or not isinstance(history.get("runs"), list):
        raise ValueError(f"Invalid retrieval result history: {results_file}")

    history["runs"].append(run_result)
    results_file.parent.mkdir(parents=True, exist_ok=True)
    temporary_file = results_file.with_suffix(f"{results_file.suffix}.tmp")
    temporary_file.write_text(
        json.dumps(history, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_file.replace(results_file)


def _format_summary(
    total_queries: int,
    results_stats: dict[str, Any],
    elapsed_time: float,
) -> str:
    """Format overall summary statistics."""
    lines = []
    lines.append("\n" + "=" * 80)
    lines.append("SUMMARY")
    lines.append("=" * 80)
    lines.append(f"Total queries: {total_queries}")
    lines.append(f"Queries with results: {results_stats['queries_with_results']}")
    lines.append(f"Queries with no results: {results_stats['queries_without_results']}")
    lines.append(
        f"Average results per query: {results_stats['avg_results_per_query']:.2f}"
    )
    lines.append(f"Total documents retrieved: {results_stats['total_results']}")
    if results_stats["labelled_queries"]:
        k = results_stats["recall_k"]
        lines.append("")
        lines.append(f"Recall@{k}: {results_stats['recall_at_k']:.3f}")
        lines.append(
            f"Labelled hits: {results_stats['labelled_hits']}/"
            f"{results_stats['labelled_queries']}"
        )
    if results_stats["errors"]:
        lines.append(f"Query errors: {results_stats['errors']}")

    # Greeting stats
    if results_stats["greeting_tested"] > 0:
        lines.append("")
        lines.append("Greeting queries (should have no results):")
        lines.append(f"  Tested: {results_stats['greeting_tested']}")
        lines.append(f"  With results: {results_stats['greeting_with_results']}")
        lines.append(
            f"  Without results: {results_stats['greeting_tested'] - results_stats['greeting_with_results']}"
        )

    lines.append("")
    lines.append(f"Elapsed time: {elapsed_time:.2f}s")
    lines.append(f"Average time per query: {elapsed_time / total_queries:.2f}s")
    lines.append("=" * 80)
    return "\n".join(lines)


def _run_retrieval_test(
    queries: list[dict[str, str]],
    match_count: int,
    match_threshold: float,
) -> dict[str, Any]:
    """
    Run retrieval tests on a list of queries.

    Args:
        queries: List of query dictionaries with 'category' and 'query' keys
        match_count: Number of results to retrieve per query
        match_threshold: Minimum similarity threshold

    Returns:
        Structured metrics and per-query outcomes suitable for JSON history.
    """
    print("\n" + "=" * 80)
    print("BedtimeNews Retriever Test")
    print("=" * 80)
    print(f"Test started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Total queries to test: {len(queries)}")
    print(f"Match count: {match_count}")
    print(f"Match threshold: {match_threshold}")
    print("=" * 80)
    print()

    results_stats = {
        "queries_with_results": 0,
        "queries_without_results": 0,
        "total_results": 0,
        "avg_results_per_query": 0.0,
        "greeting_tested": 0,
        "greeting_with_results": 0,
        "errors": 0,
        "labelled_queries": 0,
        "labelled_hits": 0,
        "recall_k": match_count,
        "recall_at_k": 0.0,
    }
    query_results = []

    started_at = datetime.now(UTC)
    start_time = datetime.now()

    for i, query_info in enumerate(queries, 1):
        query = query_info["query"]
        category = query_info.get("category", "unknown")
        relevant_doc_id = query_info.get("doc_id")
        if relevant_doc_id:
            results_stats["labelled_queries"] += 1

        print(f"\n[{i}/{len(queries)}] Category: {category}")
        print(f"Testing: {query[:80]}...")

        try:
            # Create retrieval request
            request = RetrieveRequest(
                query=query,
                match_count=match_count,
                match_threshold=match_threshold,
                include_text=False,
                include_heading=True,
            )

            # Perform retrieval
            response = retriever.retrieve(request)
            results = response.results
            retrieved_doc_ids = [result.doc_id for result in results]
            relevant_rank = next(
                (
                    rank
                    for rank, doc_id in enumerate(retrieved_doc_ids, start=1)
                    if doc_id == relevant_doc_id
                ),
                None,
            )
            hit = relevant_rank is not None
            if relevant_doc_id and hit:
                results_stats["labelled_hits"] += 1

            # Track category-specific stats
            is_greeting = category == "greeting"

            if is_greeting:
                results_stats["greeting_tested"] += 1
                if results:
                    results_stats["greeting_with_results"] += 1

            # Update statistics
            results_stats["total_results"] += len(results)
            if results:
                results_stats["queries_with_results"] += 1
                if is_greeting:
                    print(f"Found {len(results)} results (unexpected for {category})")
                else:
                    print(f"Found {len(results)} results")
            else:
                results_stats["queries_without_results"] += 1
                if is_greeting:
                    print("No results found (expected)")
                else:
                    print("No results found")

            query_results.append(
                {
                    "category": category,
                    "query": query,
                    "relevant_doc_id": relevant_doc_id,
                    "retrieved_doc_ids": retrieved_doc_ids,
                    "relevant_rank": relevant_rank,
                    "hit": hit if relevant_doc_id else None,
                }
            )
        except Exception as e:
            error_msg = f"Error: {str(e)}"
            print(error_msg)
            results_stats["errors"] += 1
            query_results.append(
                {
                    "category": category,
                    "query": query,
                    "relevant_doc_id": relevant_doc_id,
                    "retrieved_doc_ids": [],
                    "relevant_rank": None,
                    "hit": False if relevant_doc_id else None,
                    "error": str(e),
                }
            )

    end_time = datetime.now()
    elapsed_time = (end_time - start_time).total_seconds()

    # Calculate average results per query
    results_stats["avg_results_per_query"] = (
        results_stats["total_results"] / len(queries) if queries else 0.0
    )
    results_stats["recall_at_k"] = (
        results_stats["labelled_hits"] / results_stats["labelled_queries"]
        if results_stats["labelled_queries"]
        else 0.0
    )

    # Format and print summary
    summary = _format_summary(len(queries), results_stats, elapsed_time)
    print(summary)

    return {
        "started_at": started_at.isoformat(),
        "query_count": len(queries),
        "parameters": {
            "match_count": match_count,
            "match_threshold": match_threshold,
        },
        "metrics": {
            f"recall@{match_count}": round(results_stats["recall_at_k"], 6),
            "hits": results_stats["labelled_hits"],
            "labelled_queries": results_stats["labelled_queries"],
            "average_results_per_query": round(
                results_stats["avg_results_per_query"], 6
            ),
            "query_errors": results_stats["errors"],
            "elapsed_seconds": elapsed_time,
        },
        "queries": query_results,
    }


if __name__ == "__main__":
    main()
