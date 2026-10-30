"""Regression evaluation of keyword search against eval_search_labels.json.

A manual harness, not an automated test. It ranks every labelled query on one
snapshot and reports:

- contains / contains_all: precision of the whole result list, precision of
  the first 10, and recall, against every transcript whose visible body holds
  the label's strings (computed from the snapshot, independent of the analyzer);
- questions (these labels plus eval_retriever_labels.json): reciprocal rank of
  the first labelled transcript, hit@10 and hit@20;
- suggestions: whether the corrected query is the expected one.

Usage:
    docker compose run --rm --build \\
      --volume ./agent/eval_results:/app/eval_results \\
      agent python -m src.eval_search --run-label my-change
    # Try ranking constants without editing code:
    python -m src.eval_search --set search_db.PROXIMITY_BONUS=1.15 --no-save
"""

import argparse
import json
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from psycopg2 import sql

from . import search, search_db, search_semantic
from .search_text import normalize
from .snapshots import Snapshot, snapshot_manager
from .vector_db import read_cursor

LABELS_FILE = Path(__file__).with_name("eval_search_labels.json")
RETRIEVER_LABELS_FILE = Path(__file__).with_name("eval_retriever_labels.json")


def _results_file() -> Path:
    local = Path(__file__).resolve().parents[1] / "eval_results"
    return (
        local / "search.json"
        if local.is_dir()
        else Path("/var/log/agent/eval_results/search.json")
    )


def load_labels() -> dict[str, Any]:
    labels = json.loads(LABELS_FILE.read_text(encoding="utf-8"))
    retriever = json.loads(RETRIEVER_LABELS_FILE.read_text(encoding="utf-8"))
    labels["questions"] = labels["questions"] + [
        {"query": item["query"], "relevant": [item["doc_id"]]}
        for item in retriever["queries"]
    ]
    return labels


def _truth(db, schema: str, strings: list[str], every: bool) -> set[str]:
    patterns = [f"%{normalize(s)}%" for s in strings]
    operator = sql.SQL("ALL") if every else sql.SQL("ANY")
    db.execute(
        sql.SQL(
            "SELECT doc_id FROM {s}.search_documents WHERE normalized_text LIKE {op}(%s)"
        ).format(s=sql.Identifier(schema), op=operator),
        (patterns,),
    )
    return {row["doc_id"] for row in db.fetchall()}


def _ranked(db, snapshot: Snapshot, query: str) -> search.ResultSet:
    return search.build_result_set(db, snapshot, query, search.parse_units(query))


def evaluate(snapshot: Snapshot, labels: dict[str, Any]) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    with read_cursor() as db:
        for kind, key, every in (
            ("contains", "any", False),
            ("contains_all", "all", True),
        ):
            for label in labels[kind]:
                truth = _truth(db, snapshot.schema_name, label[key], every)
                ids = [e.doc_id for e in _ranked(db, snapshot, label["query"]).entries]
                hits = [doc_id in truth for doc_id in ids]
                cases.append(
                    {
                        "kind": kind,
                        "query": label["query"],
                        "results": len(ids),
                        "truth": len(truth),
                        "precision": sum(hits) / len(ids) if ids else 1.0,
                        "precision_at_10": sum(hits[:10]) / min(10, len(ids))
                        if ids
                        else 1.0,
                        "recall": sum(hits) / len(truth) if truth else 1.0,
                    }
                )
        for label in labels["questions"]:
            result = _ranked(db, snapshot, label["query"])
            ids = [e.doc_id for e in result.entries]
            rank = next(
                (i for i, doc_id in enumerate(ids, 1) if doc_id in label["relevant"]),
                None,
            )
            cases.append(
                {
                    "kind": "question",
                    "query": label["query"],
                    "mode": result.mode,
                    "reranked": result.reranked,
                    "results": len(ids),
                    "rank": rank,
                    "reciprocal_rank": 1 / rank if rank else 0.0,
                }
            )
        for label in labels["suggestions"]:
            result = _ranked(db, snapshot, label["query"])
            cases.append(
                {
                    "kind": "suggestion",
                    "query": label["query"],
                    "suggestion": result.suggestion,
                    "correct": result.suggestion == label["expected"],
                }
            )
    return {"summary": summarize(cases), "cases": cases}


def summarize(cases: list[dict[str, Any]]) -> dict[str, float]:
    def mean(kind: str, field: str) -> float:
        values = [c[field] for c in cases if c["kind"] in kind.split("|")]
        return round(statistics.fmean(values), 4) if values else 0.0

    questions = [c for c in cases if c["kind"] == "question"]
    return {
        "precision": mean("contains|contains_all", "precision"),
        "precision_at_10": mean("contains|contains_all", "precision_at_10"),
        "recall": mean("contains|contains_all", "recall"),
        "question_mrr": mean("question", "reciprocal_rank"),
        "question_hit_at_10": round(
            sum(bool(c["rank"] and c["rank"] <= 10) for c in questions)
            / max(1, len(questions)),
            4,
        ),
        "question_hit_at_20": round(
            sum(bool(c["rank"] and c["rank"] <= 20) for c in questions)
            / max(1, len(questions)),
            4,
        ),
        "suggestion_accuracy": mean("suggestion", "correct"),
    }


def _apply(assignments: list[str]) -> dict[str, str]:
    modules = {
        "search": search,
        "search_db": search_db,
        "search_semantic": search_semantic,
    }
    applied = {}
    for assignment in assignments:
        name, value = assignment.split("=", 1)
        module, attribute = name.split(".", 1)
        current = getattr(modules[module], attribute)
        setattr(modules[module], attribute, type(current)(value))
        applied[name] = value
    return applied


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", help="Snapshot id (default: current)")
    parser.add_argument("--run-label", default="")
    parser.add_argument(
        "--set", action="append", default=[], metavar="MODULE.NAME=VALUE"
    )
    parser.add_argument("--no-save", action="store_true")
    parser.add_argument("--cases", action="store_true", help="Print every case")
    args = parser.parse_args()
    overrides = _apply(args.set)
    if args.snapshot:
        snapshot = Snapshot(
            args.snapshot, f"rag_{args.snapshot}", search.SEARCH_FORMAT, ""
        )
    else:
        snapshot_manager.refresh()
        snapshot = snapshot_manager.require()
    report = evaluate(snapshot, load_labels())
    if args.cases:
        for case in report["cases"]:
            print(json.dumps(case, ensure_ascii=False))
    print(json.dumps(report["summary"], indent=2))
    if args.no_save:
        return
    path = _results_file()
    runs = json.loads(path.read_text()) if path.exists() else []
    runs.append(
        {
            "run_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "run_label": args.run_label,
            "snapshot_id": snapshot.snapshot_id,
            "overrides": overrides,
            "rerank": search_semantic.rerank_enabled(),
            **report,
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(runs, ensure_ascii=False, indent=2) + "\n")
    print(f"saved to {path}", file=sys.stderr)


if __name__ == "__main__":
    main()
