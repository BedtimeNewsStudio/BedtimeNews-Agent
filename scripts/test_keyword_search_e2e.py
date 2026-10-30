"""HTTP checks against the running local sample and its source files.

Run after building the sample indexer and agent:
    uv run --no-sync python scripts/test_keyword_search_e2e.py [--lifecycle]

The oracle is rebuilt from the sample source checkout inside the indexer, never
from the search tables. Golden projection tests separately check Markdown rules.
Only localhost HTTP endpoints and an INDEXER_SCOPE=sample / *_local container
are accepted. By default checks are read-only. --lifecycle additionally builds a
snapshot and checks retirement/rollback, then restores the new snapshot. Neither
mode edits transcripts or credentials.
"""

import argparse
import base64
import json
import math
import statistics
import subprocess
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import urlopen

ORACLE_CODE = """
import json
from src.settings import settings
from src.file_scanner import scan_files
from src.document_loader import load_indexable_source
from src.search_projection import project_search
from src.paths import CONTENTS_DIR
from src.transcript_export import project_transcript
if settings.indexer_scope != 'sample' or not settings.postgres_db.endswith('_local'):
    raise RuntimeError('E2E checks require sample scope and a database ending in _local')
documents = {}
for uri in sorted(scan_files()):
    p = project_search(load_indexable_source(uri))
    title = project_transcript(uri, (CONTENTS_DIR / uri).read_bytes(), '').source_title
    documents[uri] = {'text': p.body_text, 'normalized': p.normalized_text, 'sections': p.sections, 'token_data': p.token_data, 'token_count': p.token_count, 'source_title': title}
print(json.dumps(documents, ensure_ascii=True))
"""


class Anchors(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.ids = set()
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.ids.update(value for name, value in attrs if name == "id")


def indexed_proximity(doc, positions, terms):
    """Independent linear merge over stored word offsets, not substring matches."""
    for first, second in zip(terms, terms[1:], strict=False):
        starts = positions.get(first, [])
        index = 0
        first_end = None
        for second_start in positions.get(second, []):
            while index < len(starts) and starts[index] + len(first) <= second_start:
                first_end = starts[index] + len(first)
                index += 1
            if (
                first_end is not None
                and second_start - first_end <= 12
                and "\n" not in doc["normalized"][first_end:second_start]
            ):
                return True
    return False


def main():
    if not __debug__:
        raise RuntimeError(
            "E2E checks require assertions; run without -O or PYTHONOPTIMIZE"
        )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", help="Save natural-language recall results as JSON")
    parser.add_argument("--url", default="http://127.0.0.1:18001")
    parser.add_argument("--indexer-container", default="bedtimenews-sample-indexer")
    parser.add_argument(
        "--lifecycle",
        action="store_true",
        help="Also publish and retire/unretire a local sample snapshot",
    )
    args = parser.parse_args()
    target = urlparse(args.url)
    if target.scheme not in {"http", "https"} or target.hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise ValueError("local HTTP only")
    output = subprocess.check_output(
        ["docker", "exec", args.indexer_container, "python", "-c", ORACLE_CODE],
        text=True,
    )
    docs = json.loads(output)
    assert docs, "sample corpus is empty"
    times = []
    checks = 0

    def get(path, params=None, expected=200):
        nonlocal checks
        url = args.url.rstrip("/") + path
        if params:
            url += "?" + urlencode(params)
        started = time.perf_counter()
        try:
            with urlopen(url, timeout=30) as response:
                status, data = response.status, json.load(response)
        except HTTPError as error:
            status, data = error.code, json.load(error)
        times.append((time.perf_counter() - started) * 1000)
        assert status == expected, (url, status, data)
        checks += 1
        return data

    health = get("/health")
    assert health["snapshot"]["format_version"] == 4
    snapshot = health["snapshot"]["id"]
    nonempty = [d for d in docs.values() if d["token_count"]]
    average = sum(d["token_count"] for d in nonempty) / len(nonempty)
    ascii_lower = str.maketrans(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"
    )

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
    from src.search import RELATIVE_CUTOFF, TITLE_BONUS
    from src.search_db import COMMON_SHARE, COVERAGE_FLOOR, PROXIMITY_BONUS
    from src.search_query import is_question, query_units, split_phrases
    from src.search_text import analyze, query_terms

    def positions_of(offsets, variant):
        """Variant starts where every word sits at its offset; independent of SQL."""
        words = {word: set(offsets.get(word, [])) for word, _ in variant.parts}
        anchor, anchor_offset = variant.anchor
        return [
            p - anchor_offset
            for p in offsets.get(anchor, [])
            if p >= anchor_offset
            and all(p - anchor_offset + o in words[w] for w, o in variant.parts)
        ]

    titles = {uri: analyze(doc["source_title"]) for uri, doc in docs.items()}

    def rank(units):
        weights = {u.term: u.weight for u in units}
        found = {}
        for uri, doc in docs.items():
            found[uri] = {}
            for unit in units:
                hits = {
                    v.text: h
                    for v in unit.variants
                    if (h := positions_of(doc["token_data"], v))
                }
                if hits:
                    found[uri][unit.term] = hits
        frequencies = {
            u.term: sum(u.term in hits for hits in found.values()) for u in units
        }
        tiers = {
            u.term: 1
            if u.core and frequencies[u.term] <= COMMON_SHARE * len(nonempty)
            else 2
            if u.core
            else 3
            for u in units
            if frequencies[u.term]
        }
        gate = {
            t for t, tier in tiers.items() if tier == min(tiers.values(), default=0)
        }
        k = len(gate)
        rule = k if k <= 2 else 2 if k <= 4 else math.ceil(k / 2)
        gate_hits = {uri: len(gate & hits.keys()) for uri, hits in found.items()}
        required = max(1, min(rule, max(gate_hits.values(), default=0)))
        body = {}
        for uri, hits in found.items():
            if not hits or gate_hits[uri] < required:
                continue
            doc = docs[uri]
            score = 0
            for term, variants in hits.items():
                tf = sum(map(len, variants.values()))
                df = frequencies[term]
                score += (
                    weights[term]
                    * math.log(1 + (len(nonempty) - df + 0.5) / (df + 0.5))
                    * tf
                    * 2.2
                    / (tf + 1.2 * (0.25 + 0.75 * doc["token_count"] / average))
                )
            coverage = sum(weights[t] for t in hits) / sum(weights.values())
            score *= COVERAGE_FLOOR + (1 - COVERAGE_FLOOR) * coverage
            if near(doc, hits, units):
                score *= PROXIMITY_BONUS
            body[uri] = score
        best = max(body.values(), default=0)
        scored = []
        for uri, score in body.items():
            if score < RELATIVE_CUTOFF * best:
                continue
            in_title = sum(
                u.weight
                for u in units
                if any(positions_of(titles[uri], v) for v in u.variants)
            )
            scored.append(
                (uri, score * (1 + TITLE_BONUS * in_title / sum(weights.values())))
            )
        matched = {
            uri: [u.term for u in units if u.term in found[uri]] for uri, _ in scored
        }
        return sorted(scored, key=lambda row: (-row[1], row[0])), matched

    def near(doc, hits, units):
        """Consecutive units in query order within 12 characters on one line."""
        for first, second in zip(units, units[1:], strict=False):
            ends = sorted(
                p + len(text)
                for text, starts in hits.get(first.term, {}).items()
                for p in starts
            )
            for starts in hits.get(second.term, {}).values():
                for start in starts:
                    before = [e for e in ends if e <= start]
                    if (
                        before
                        and start - before[-1] <= 12
                        and "\n" not in doc["normalized"][before[-1] : start]
                    ):
                        return True
        return False

    def oracle(query):
        units = query_units(query)
        expected, matched = rank(units)
        split = split_phrases(units)
        if not expected and split and split != units:
            expected, matched = rank(split)
        return expected, [u.term for u in units], matched, is_question(query, units)

    queries = [
        "债",
        "债务",
        "贵州",
        "贵州 债务",
        "地方债务",
        "房地产",
        "OPENAI",
        "100%",
        "a_b",
        "zzqxnonexistentkeywordpvelx",
        "  债务\u3000债务  ",
    ]
    natural_cases = [
        (
            "贵州的债务问题现状及其成因",
            ["ShuiQianXiaoXi/0501-0600/0581.md", "ShuiQianXiaoXi/0501-0600/0588.md"],
        ),
        (
            "地方政府城投债为什么偿还困难",
            ["ShuiQianXiaoXi/0501-0600/0581.md", "ShuiQianXiaoXi/0501-0600/0588.md"],
        ),
        ("恒大房地产危机对经济有什么影响", ["ShuiQianXiaoXi/0301-0400/0329.md"]),
        ("中国人口出生率下降有哪些原因", ["ShuiQianXiaoXi/0401-0500/0461.md"]),
        (
            "人工智能发展对就业会有什么影响",
            ["ShuiQianXiaoXi/0001-0100/0012.md", "CanKaoXinXi/0201-0300/0266.md"],
        ),
        (
            "高铁建设是否会带来地方财政压力",
            ["ShuiQianXiaoXi/0201-0300/0280.md", "CanKaoXinXi/0501-0600/0515.md"],
        ),
    ]
    queries.extend(query for query, _ in natural_cases)
    recall_results = []

    def check_keyword(query, data, expected, matched):
        assert data["mode"] == "keyword"
        assert data["total"] == len(expected), (query, data["total"], len(expected))
        assert [r["doc_id"] for r in data["results"]] == [
            uri for uri, _ in expected[:50]
        ], query
        for result, (_, score) in zip(data["results"], expected, strict=False):
            assert math.isclose(result["score"], score, rel_tol=1e-12), (query, result)
            assert result["matched_terms"] == matched[result["doc_id"]]
            assert result["match"] == "keyword"
            assert result["title"] and result["source_title"]
            assert 1 <= len(result["snippets"]) <= 3
            doc = docs[result["doc_id"]]
            for snippet in result["snippets"]:
                anchor = snippet["heading_anchor"]
                assert any(s["heading_anchor"] == anchor for s in doc["sections"])
                for part in snippet["parts"]:
                    if part["highlight"]:
                        text = part["text"].translate(ascii_lower)
                        assert text in doc["normalized"], (query, part)

    for query in queries:
        expected, terms, matched, question = oracle(query)
        data = get("/search", {"q": query, "limit": 50})
        assert data["snapshot_id"] == snapshot
        assert data["terms"] == terms
        if question:
            # Semantic recall depends on the embedding model, so a question is
            # not checked score by score: every keyword result must remain, and
            # the labelled episode must lead (below).
            assert data["mode"] in {"keyword", "hybrid"}, data["mode"]
            if data["total"] <= 50:
                listed = {r["doc_id"] for r in data["results"]}
                assert {uri for uri, _ in expected} <= listed, query
        else:
            check_keyword(query, data, expected, matched)
        print(f"PASS {query!r}: {data['total']} transcripts ({data['mode']})")
        if query in {q for q, _ in natural_cases}:
            relevant = next(ids for q, ids in natural_cases if q == query)
            top_ids = [r["doc_id"] for r in data["results"][:3]]
            assert any(uri in relevant for uri in top_ids), (query, top_ids)
            recall_results.append(
                {
                    "query": query,
                    "snapshot_id": data["snapshot_id"],
                    "mode": data["mode"],
                    "terms": data["terms"],
                    "total": data["total"],
                    "expected_relevant": relevant,
                    "hit_at_1": top_ids[0] in relevant,
                    "hit_at_3": True,
                    "known_relevant_recall_at_3": sum(
                        uri in top_ids for uri in relevant
                    )
                    / len(relevant),
                    "top_results": data["results"][:3],
                }
            )
            for result in data["results"][:3]:
                print(
                    "  ",
                    result["source_title"],
                    result["match"],
                    result["matched_terms"],
                    round(result["score"], 4),
                )

    # Full traversal must have the same ranking and no repeated documents.
    expected, _, _, _ = oracle("债务")
    page = get("/search", {"q": "债务", "limit": 1})
    cursor = page["next_cursor"]
    seen = [r["doc_id"] for r in page["results"]]
    while cursor:
        page = get("/search", {"q": "债务", "limit": 1, "cursor": cursor})
        assert page["snapshot_id"] == snapshot and page["total"] == len(expected)
        seen.extend(r["doc_id"] for r in page["results"])
        cursor = page["next_cursor"]
    assert seen == [uri for uri, _ in expected]
    assert len(seen) == len(set(seen))

    # Order, filters and facets slice the same ranked list.
    everything = get("/search", {"q": "债务", "limit": 50})
    by_date = get("/search", {"q": "债务", "limit": 50, "sort": "date"})
    assert sorted(r["doc_id"] for r in by_date["results"]) == sorted(
        r["doc_id"] for r in everything["results"]
    )
    dates = [r["publication_date"] or "" for r in by_date["results"]]
    assert dates == sorted(dates, reverse=True)
    for facet in everything["facets"]["channels"]:
        only = get("/search", {"q": "债务", "limit": 50, "channel": facet["value"]})
        assert only["total"] == facet["count"]
        assert {r["channel"] for r in only["results"]} == {facet["value"]}
    print("PASS order, channel filters and facets")

    # Snippet links must correspond to real IDs in the existing reader HTML.
    data = get("/search", {"q": "债务", "limit": 50})
    for result in data["results"]:
        detail = get("/transcripts/" + quote(result["doc_id"], safe="/"))
        anchors = Anchors(detail["body_html"]).ids
        assert all(
            not s["heading_anchor"] or s["heading_anchor"] in anchors
            for s in result["snippets"]
        )

    # Terms found only outside a body must not make their source episode match.
    appendix_case = subprocess.check_output(
        [
            "docker",
            "exec",
            args.indexer_container,
            "python",
            "-c",
            "import json,re; from src.file_scanner import scan_files; from src.paths import CONTENTS_DIR; "
            "print(json.dumps({u: (CONTENTS_DIR/u).read_text().split('## 附录',1)[-1] for u in scan_files()}))",
        ],
        text=True,
    )
    for uri, appendix in json.loads(appendix_case).items():
        candidates = [term for term in query_terms(appendix) if 2 <= len(term) <= 64]
        term = next((t for t in candidates if t not in docs[uri]["token_data"]), None)
        if term:
            result = get("/search", {"q": term, "limit": 50})
            assert uri not in {r["doc_id"] for r in result["results"]}
            print(f"PASS appendix exclusion: {term!r}")
            break

    for params in [
        {"q": " "},
        {"q": "x" * 65},
        {"q": "的和及其"},
        {"q": "!!!"},
        {"q": "债务", "limit": 51},
        {"q": "债务", "cursor": "!"},
        {"q": "\x00"},
    ]:
        get("/search", params, expected=422)
    first = get("/search", {"q": "债务", "limit": 1})
    assert first["next_cursor"], "sample must contain multiple transcripts with 债务"
    get(
        "/search",
        {"q": "高铁", "limit": 1, "cursor": first["next_cursor"]},
        expected=422,
    )
    expired = json.loads(
        base64.urlsafe_b64decode(
            first["next_cursor"] + "=" * (-len(first["next_cursor"]) % 4)
        )
    )
    for invalid in [
        dict(expired, snapshot_id="bad\x00id"),
        dict(expired, version=expired["version"] - 1),
    ]:
        invalid_cursor = (
            base64.urlsafe_b64encode(json.dumps(invalid).encode()).decode().rstrip("=")
        )
        get(
            "/search", {"q": "债务", "limit": 1, "cursor": invalid_cursor}, expected=422
        )
    expired["snapshot_id"] = "s_missing_search_e2e"
    encoded = (
        base64.urlsafe_b64encode(json.dumps(expired).encode()).decode().rstrip("=")
    )
    get("/search", {"q": "债务", "limit": 1, "cursor": encoded}, expected=410)
    if args.lifecycle:

        def operation(*command):
            return subprocess.check_output(
                ["docker", "exec", args.indexer_container, "python", *command],
                text=True,
            )

        def wait_snapshot(expected_id):
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                if get("/health")["snapshot"]["id"] == expected_id:
                    return
                time.sleep(1)
            raise AssertionError(f"API did not select {expected_id}")

        output = operation(
            "-c",
            "from src.pipeline import run_once; import json; r=run_once(full=True,sync=False); assert r.result=='published',r; print(json.dumps({'snapshot_id':r.snapshot_id}))",
        )
        new_snapshot = json.loads(output.splitlines()[-1])["snapshot_id"]
        wait_snapshot(new_snapshot)
        continued = get(
            "/search", {"q": "债务", "limit": 1, "cursor": first["next_cursor"]}
        )
        assert continued["snapshot_id"] == snapshot
        new_page = get("/search", {"q": "债务", "limit": 1})
        assert new_page["snapshot_id"] == new_snapshot
        try:
            operation("-m", "src.snapshots", "retire", new_snapshot)
            get(
                "/search",
                {"q": "债务", "limit": 1, "cursor": new_page["next_cursor"]},
                expected=410,
            )
            wait_snapshot(snapshot)
            assert get("/search", {"q": "债务", "limit": 1})["snapshot_id"] == snapshot
        finally:
            operation("-m", "src.snapshots", "unretire", new_snapshot)
        wait_snapshot(new_snapshot)
        print(
            "PASS publication, cursor continuity, retirement, rollback, and restoration"
        )
        snapshot = new_snapshot
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(
            json.dumps(
                {
                    "snapshot_id": snapshot,
                    "sample_transcripts": len(docs),
                    "http_checks": checks,
                    "cases": recall_results,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
    print(
        json.dumps(
            {
                "status": "passed",
                "snapshot_id": snapshot,
                "sample_transcripts": len(docs),
                "http_checks": checks,
                "http_p50_ms": round(statistics.median(times), 2),
                "http_p95_ms": round(
                    sorted(times)[math.ceil(len(times) * 0.95) - 1], 2
                ),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
