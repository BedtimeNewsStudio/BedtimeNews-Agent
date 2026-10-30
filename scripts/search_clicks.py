"""Summarize anonymous search-result clicks from the web service log.

    docker compose logs --no-log-prefix web | uv run --no-sync python scripts/search_clicks.py

The 搜索 pane reports each opened result (query, transcript, rank, match kind,
mode) to /api/search/click, which logs it as `search_click {json}`. This prints
where clicks land in the ranking, how often semantic-only results are chosen,
and the queries whose clicks land furthest down: the first places to look when
tuning ranking or adding labels to agent/src/eval_search_labels.json.
"""

import json
import statistics
import sys
from collections import Counter, defaultdict

MARKER = "search_click "


def main() -> None:
    clicks = []
    for line in sys.stdin:
        at = line.find(MARKER)
        if at >= 0:
            try:
                clicks.append(json.loads(line[at + len(MARKER) :]))
            except json.JSONDecodeError:
                continue
    if not clicks:
        print("no search_click lines found")
        return
    ranks = [c["rank"] for c in clicks]
    print(f"clicks: {len(clicks)}  queries: {len({c['q'] for c in clicks})}")
    print(f"median clicked rank: {statistics.median(ranks)}")
    buckets = Counter(
        "1" if r == 1 else "2-3" if r <= 3 else "4-10" if r <= 10 else ">10"
        for r in ranks
    )
    for bucket in ("1", "2-3", "4-10", ">10"):
        print(f"  rank {bucket:>5}: {buckets[bucket] / len(clicks):.1%}")
    matches = Counter(c["match"] for c in clicks)
    print("by match:", dict(matches))
    by_query = defaultdict(list)
    for click in clicks:
        by_query[click["q"]].append(click["rank"])
    worst = sorted(
        by_query.items(), key=lambda kv: (-statistics.mean(kv[1]), -len(kv[1]))
    )
    print("queries whose clicks land lowest (mean rank, clicks):")
    for query, query_ranks in worst[:20]:
        print(f"  {statistics.mean(query_ranks):6.1f} {len(query_ranks):4d}  {query}")


if __name__ == "__main__":
    main()
