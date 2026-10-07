"""
Evaluation tool for agentic RAG (full pipeline).

This tool exercises the complete agentic RAG pipeline including routing, query
rewriting, retrieval, document grading, and answer generation. It is a manual
evaluation harness, not an automated test.

Usage:
    # Evaluate a single query directly
    docker compose exec agent python -m src.eval_agent --query "你的问题"
    docker compose exec agent python -m src.eval_agent -q "你的问题"

    # List available categories
    docker compose exec agent python -m src.eval_agent --list-categories

    # Evaluate all queries
    docker compose exec agent python -m src.eval_agent

    # Evaluate specific category
    docker compose exec agent python -m src.eval_agent --category education

    # Random sample of queries
    docker compose exec agent python -m src.eval_agent --random 10

"""

import argparse
import asyncio
import random
import sys
import time
from datetime import datetime
from typing import Any

from .agent import agent_query, agent_stream_query
from .eval_queries import ALL_QUERIES, CATEGORY_NAMES_CN, FLAT_QUERIES
from .snapshots import snapshot_manager


def main():
    parser = argparse.ArgumentParser(
        description="Test BedtimeNews agentic RAG pipeline",
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

    parser.add_argument(
        "--limit",
        type=int,
        help="Limit number of queries to test",
    )

    parser.add_argument(
        "--list-categories",
        action="store_true",
        help="List all available query categories and exit",
    )

    args = parser.parse_args()

    # Outside the API server nothing polls the snapshot registry: select the
    # current snapshot once, so every query of this run reads the same one.
    snapshot_manager.refresh()
    snapshot_manager.require()

    if args.list_categories:
        print("\nAvailable query categories:")
        print("=" * 80)
        for category, queries in ALL_QUERIES.items():
            cn_name = CATEGORY_NAMES_CN.get(category, "")
            print(f"  {category:25s}: {len(queries):3d} queries  # {cn_name}")
        print("=" * 80)
        print(f"\nTotal: {len(ALL_QUERIES)} categories, {len(FLAT_QUERIES)} queries")
        return

    if args.query:
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

    if args.limit:
        queries = queries[: args.limit]
        print(f"Limited to first {len(queries)} queries")

    if not queries:
        print("No queries to test", file=sys.stderr)
        sys.exit(1)

    try:
        _run_agent_test(queries=queries)
    except KeyboardInterrupt:
        print("\n\nTest interrupted by user", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"\nTest failed: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        sys.exit(1)


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
    lines.append(
        f"Average answer length: {results_stats['avg_answer_length']:.1f} chars"
    )
    lines.append(f"Elapsed time: {elapsed_time:.2f}s")
    lines.append(f"Average time per query: {elapsed_time / total_queries:.2f}s")
    lines.append("=" * 80)
    return "\n".join(lines)


def _format_result(
    query_info: dict[str, str],
    result: dict[str, Any],
    query_num: int,
    total_queries: int,
) -> str:
    """Format a single agent result for display."""
    lines = []
    lines.append("=" * 80)
    lines.append(f"Query {query_num}/{total_queries}")
    lines.append("=" * 80)
    lines.append(f"Category: {query_info['category']}")
    lines.append(f"Query: {query_info['query']}")
    lines.append("")
    lines.append("")
    lines.append("Answer:")
    lines.append("-" * 80)
    lines.append(result.get("answer", ""))
    lines.append("-" * 80)

    lines.append("")
    return "\n".join(lines)


def _run_agent_test(queries: list[dict[str, str]]) -> None:
    """
    Run agent tests on a list of queries.

    Args:
        queries: List of query dictionaries with 'category' and 'query' keys
    """
    print("\n" + "=" * 80)
    print("BedtimeNews Agentic RAG Test")
    print("=" * 80)
    print(f"Test started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Total queries to test: {len(queries)}")
    print("=" * 80)
    print()

    results_stats = {
        "total_answer_length": 0,
        "avg_answer_length": 0.0,
    }

    start_time = datetime.now()

    for i, query_info in enumerate(queries, 1):
        query = query_info["query"]

        print(f"\n[{i}/{len(queries)}] Testing: {query[:80]}...")

        try:
            query_start_time = time.perf_counter()
            result = agent_query(query)
            query_end_time = time.perf_counter()

            print(f"Query time: {query_end_time - query_start_time:.2f}s")

            answer_length = len(result.get("answer", ""))
            results_stats["total_answer_length"] += answer_length

            print(f"Completed: {answer_length} chars")

            # Print formatted result
            formatted = _format_result(query_info, result, i, len(queries))
            print(formatted)

            # Test streaming version (limited event output)
            print("\n[Streaming test...]")
            try:

                async def stream_single_query(q):
                    event_count = 0
                    async for event in agent_stream_query(q):
                        event_count += 1
                        if event_count > 100:  # Limit output
                            print("  ... (skipping remaining events)")
                            break

                        if event["type"] == "step":
                            print(f"  [Step:{event['step']}] {event['content']}")
                        elif event["type"] == "answer_chunk":
                            content = event["content"]
                            if len(content) > 50:
                                content = content[:50] + "..."
                            print(f"  [Chunk] '{content}'")

                    return event_count

                event_total = asyncio.run(stream_single_query(query))
                print(f"Streaming completed: {event_total} events")

            except Exception as e:
                print(f"Streaming error: {e}")

        except Exception as e:
            error_msg = f"Error: {str(e)}"
            print(error_msg)
            print(f"\nQuery {i}/{len(queries)}")
            print(f"Category: {query_info['category']}")
            print(f"Query: {query}")
            print(f"\n{error_msg}\n")

    end_time = datetime.now()
    elapsed_time = (end_time - start_time).total_seconds()

    # Calculate averages
    results_stats["avg_answer_length"] = (
        results_stats["total_answer_length"] / len(queries) if queries else 0.0
    )

    # Format and print summary
    summary = _format_summary(len(queries), results_stats, elapsed_time)
    print(summary)


if __name__ == "__main__":
    main()
