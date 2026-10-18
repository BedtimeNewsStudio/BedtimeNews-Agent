# Keyword Search Backend: Chinese Word Indexing and API

Status: complete within the backend-only scope. The local implementation uses
word segmentation as its sole default search behavior. Frontend exposure and
integration are outside this design. Validation uses the localhost sample;
production rollout and full-corpus capacity measurements are operational work.

## 1. Scope and behavior

Provide search over complete transcript bodies (正文), with one result per
transcript, relevance ranking, matching snippets, and snapshot-bound pagination.
The web service does not call or proxy `/search` or `/api/search` and has no search
interface. The sample overlay binds the Agent API to loopback for development.

Queries are natural-language text, whether or not the user inserts spaces:

```text
贵州的债务问题现状及其成因
    -> 贵州 / 债务 / 问题 / 现状 / 成因
```

A transcript is a candidate when at least one analyzed query word occurs in its
indexed body. A document discussing 贵州 and 债务 remains retrievable without
containing the question verbatim, or the words 现状 and 成因. There is no exact or
relaxed mode parameter, minimum two-word requirement, or automatic AND fallback.

| Property   | Behavior                                                                                |
| ---------- | --------------------------------------------------------------------------------------- |
| Analysis   | Chinese segmentation with the same pinned dictionary and normalization in both services |
| Matching   | Exact indexed words, combined with OR across the query                                  |
| Stopwords  | Grammatical/question words are removed; punctuation-only tokens are discarded           |
| Case       | ASCII Latin letters are case insensitive; display text retains its case                 |
| Repetition | Deduplicate query words; retain all document occurrences                                |
| Ranking    | Weighted BM25, query-word coverage, and a bounded proximity bonus                       |
| Metadata   | Episode title, channel, and date are displayed but not searched or scored               |
| Synonyms   | No semantic expansion, query LLM, spelling correction, or embedding call                |

The analyzer determines word boundaries. A standalone English word such as `AI`
does not match the inside of `OpenAI`. Punctuation is analyzed rather than treated
as a literal query operator. Quotes do not introduce a separate exact mode.

## 2. Lexical analysis

Both independently built images install `jieba==0.42.1`, a Python dependency
using its packaged dictionary and model files locally. No new database extension,
system daemon, or network service is introduced.

`agent/src/search_text.py` and `indexer/src/search_text.py` are byte-identical
copies because the components have separate Docker build contexts. A repository
contract test enforces equality. The pinned package version, domain dictionary,
stopwords, normalization, and segmentation rules belong to `SEARCH_VERSION`.

Normalization applies Unicode NFC and ASCII lowercasing. The visible projection
is NFC-normalized first, so normalization does not change the offsets used for
snippets. There is no simplified/traditional Chinese conversion or stemming.

The analyzer uses a fixed domain vocabulary, including 城投、城投债、地方债、恒大、
碧桂园 and 人工智能. Jieba's HMM is disabled. The complete vocabulary and stopword
lists are constants in `search_text.py`; no request mutates the dictionary.

- Body indexing uses jieba's search segmentation, including recognized subwords
  of compounds. This supports a query for a constituent word without requiring
  the body to have used the same coarser segmentation.
- Query analysis uses precise/default segmentation. This preserves meaningful
  compounds such as 房地产 and 人工智能 rather than counting all their subwords as
  separate query requirements or relevance contributions.
- Both paths remove the same stopwords and tokens containing no alphanumeric
  character. Query words retain deterministic source order and are deduplicated.

Words such as 的、及其、为什么、哪些、是否 are stopwords. General question/analysis
vocabulary such as 问题、现状、成因、原因、影响 remains searchable but has query
weight `0.15`; other words have weight `1.0`. A query consisting only of an
analysis word can still search for that word. This weighting is independent of
specific test questions or episode IDs.

## 3. Complete-body projection

`search_projection.py` reuses `load_indexable_source()` to take only the content
between `## 正文` and `## 附录`, excluding both marker lines. It does not reuse
embedding chunks: those overlap and filter short passages.

The existing MarkdownIt/table parser and standard-library HTML parser preserve
visible prose, topic titles, lists, tables, link labels, and inline/fenced code.
Link destinations, formatting markers, footnote reference markers, HTML comments,
and script/style contents are excluded. Opening prose and short topics remain
searchable. A NUL character fails the build with its document URI.

Do not use `transcripts.body_markdown` as the search input: that reader field
also includes the publication line and appendix.

Sections reuse `iter_h2()` and `assign_heading_anchors()`, including duplicate
heading disambiguation. Each nonempty section stores `start`, `end`, `heading`,
and `heading_anchor`; offsets are Unicode character offsets in the visible body.
Sections are joined with two newlines. Opening prose has an empty anchor.

## 4. Snapshot schema and positions

`FORMAT_VERSION = 4`, `SEARCH_VERSION = 2`. Every snapshot contains:

```sql
CREATE TABLE <snapshot>.search_documents (
    doc_id          TEXT PRIMARY KEY,
    body_text       TEXT NOT NULL,
    normalized_text TEXT NOT NULL,
    token_count     INTEGER NOT NULL CHECK (token_count >= 0),
    token_data      JSONB NOT NULL,
    search_terms    TSVECTOR NOT NULL,
    body_hash       VARCHAR(64) NOT NULL,
    search_version  INTEGER NOT NULL,
    sections        JSONB NOT NULL
);

CREATE TABLE <snapshot>.search_statistics (
    id                  BOOLEAN PRIMARY KEY DEFAULT true CHECK (id),
    document_count      INTEGER NOT NULL CHECK (document_count >= 0),
    average_token_count DOUBLE PRECISION NOT NULL CHECK (average_token_count >= 0),
    search_version      INTEGER NOT NULL
);

CREATE INDEX idx_search_documents_terms
ON <snapshot>.search_documents USING gin (search_terms);
```

`<snapshot>` is composed with `psycopg2.sql.Identifier`. `body_hash` follows the
existing extracted-body hash used for build change detection.

`token_data` maps each normalized word to its sorted, deduplicated occurrence
starts, for example `{"贵州": [0, 8], "债务": [3, 10]}`. Each word's frequency is
the array length; an occurrence ends at its start plus the word's character
length. Different words can overlap at the same character position. `token_count`
is the sum of the occurrence counts, including indexed compound subwords.

A word's index lexeme is `w` followed by the hexadecimal SHA-256 of its UTF-8
text. `array_to_tsvector()` stores these unique fixed-size lexemes without
positions. Matching additionally checks the raw word key in `token_data`, so
hashes are candidate keys, not substitutes for word equality.

Complete offsets live in JSONB instead of positional `tsvector` entries. This
avoids truncating occurrences at PostgreSQL's 16,383 position / 256 positions per
lexeme limits. Tests cover 17,000 occurrences of one word. PostgreSQL's overall
vector-size limit still applies: insertion errors identify the document and roll
back publication; bodies are never truncated to fit.

The one statistics row is regenerated on every publication from documents whose
`token_count > 0`. Search document rows are copied incrementally; statistics are
not copied from the base.

## 5. Retrieval and ranking

`search_db.py::query_search()` performs candidate retrieval, frequency collection,
scoring, sorting, and page selection in one parameterized SQL statement:

1. `input` pairs ordered query words with their lexemes and query weights.
2. `matches AS MATERIALIZED` joins each word through `search_terms @@ query` and
   verifies `token_data ? term`. Any matching word admits a document.
3. `frequencies` computes each matched word's document frequency across the whole
   snapshot, rather than only documents matching all query words.
4. `base AS MATERIALIZED` groups by document, sums weighted BM25 contributions,
   and applies the query coverage factor.
5. `ranked AS MATERIALIZED` applies the proximity bonus to all candidates.
6. `page` joins titles/reader metadata, sorts, then applies `LIMIT` / `OFFSET`.
   The final row includes both the complete candidate count and the requested
   page, including a correct total for an empty page.

No arbitrary candidate limit precedes ranking. GIN is available for word lookup;
PostgreSQL can choose a sequential scan for a small or low-selectivity corpus.

For query word `t` and document `d`:

```text
idf(t) = ln(1 + (N - df(t) + 0.5) / (df(t) + 0.5))

bm25(d) = sum over matched query words:
    weight(t) * idf(t) * tf(t,d) * 2.2
    / (tf(t,d) + 1.2 * (0.25 + 0.75 * token_count(d) / average_token_count))

coverage(d) = sum(weight(t) for matched words) / sum(weight(t) for all query words)

score(d) = bm25(d) * (0.25 + 0.75 * coverage(d)) * proximity(d)
```

`N` and average length come from `search_statistics`; `tf` is the indexed word's
occurrence count, and `df` is the corpus-wide exact word document frequency.
Unknown query words do not exclude documents. They can lower coverage but do not
force an empty response. Empty token corpora return an empty result safely.

`proximity(d)` is `1.15` when at least two query words match and an adjacent pair
in query order occurs in the normalized body in that order, separated by at most
12 non-newline characters; otherwise it is `1.0`. Escaped, bound regex patterns
only prefilter possible proximity. The final decision uses actual indexed word
starts and ends, preventing `ai` inside `openai` from earning an `ai` proximity
bonus. SQL merges first-word end and second-word start events with a running
maximum, avoiding a Cartesian join of occurrence arrays. This is an optional
ranking contribution, not a phrase-match condition.

Order by full-precision score descending, then `doc_id COLLATE "C"` ascending.
Scores are returned without application rounding and are not confidence values.
Any-word recall intentionally includes weak matches at the tail. There is no
hardcoded result-count minimum, coverage cutoff, or undocumented score threshold.

## 6. Snippets

Only the requested page receives snippets. SQL returns each result's ordered
`matched_terms` and the corresponding stored offsets; it does not transfer the
entire token dictionary. Snippets use these exact word occurrences, preventing
substring highlighting inside a word that did not match the index.

The Python selector evaluates nominal 120-character windows at 40-character
buckets within each topic. It prefers distinct query-word weight coverage,
then weighted occurrence density, then position. Binary searches bound lookup
work. Windows expand to preserve matched words and seek a sentence/newline or
section boundary within 24 characters. Overlapping windows merge when their
combined span is at most 200 characters; at most three windows remain.

Overlapping/adjacent highlights merge. The API returns literal text/highlight
parts and the existing topic anchor, preserving case and adding ellipses where
content is omitted. This contract identifies a document and topic; rendering and
click navigation belong to a future frontend integration.

## 7. API contract and pagination

```http
GET /search?q=贵州的债务问题现状及其成因&limit=20
```

| Parameter | Contract                                                                                                          |
| --------- | ----------------------------------------------------------------------------------------------------------------- |
| `q`       | Required; 1–512 Unicode characters, no NUL; analyze to 1–64 distinct searchable words, each at most 64 characters |
| `limit`   | Optional, default 20, range 1–50                                                                                  |
| `cursor`  | Optional, at most 16,384 characters; retain the same analyzed word order and page size                            |

The response contains original `query`, analyzed `terms`, `snapshot_id`, total
candidate count `total`, ranked `results`, and nullable `next_cursor`. Each result
contains `doc_id`, standardized `title`, actual `source_title`, channel, date,
score, ordered `matched_terms`, and up to three snippets. A result can match only
some of `terms`; `matched_terms` makes that explicit. There is no `mode` parameter.

| HTTP status | Meaning                                                                                                   |
| ----------- | --------------------------------------------------------------------------------------------------------- |
| 200         | Results, including an empty list if no indexed query word matches                                         |
| 410         | Cursor snapshot missing, retired, dropped, or incompatible; restart search                                |
| 422         | Invalid limits/cursor/query, no searchable words after analysis, or changed analyzed word order/page size |
| 503         | Current search index not ready, including pre-format-4 snapshots, or service/database unavailable         |

Cursor version 3 is URL-safe unpadded Base64 JSON with an ASCII alphanumeric/underscore snapshot ID, analyzed
ordered words, offset, and page size. Strict Pydantic validation rejects unknown
fields/types; offset is bounded by `2**31 - 1`. It is a validated position token,
not a signed authorization token. Cursors from the previous analysis contract
are invalid; the server does not reinterpret them under new semantics. Version 3
also invalidates version 2 cursors because the corrected proximity ranking can
change result order without changing the stored index.

Initial requests use the snapshot manager's current selection. Cursor requests
resolve their snapshot through the registry and require published status, format
4, the same embedding space, and compliance with an optional `RAG_SNAPSHOT` pin.
Table `ACCESS SHARE` locks protect the read transaction from concurrent GC. A
cursor does not extend retention or silently switch snapshots.

The synchronous search runs through `asyncio.to_thread`, with the existing
read-only PostgreSQL pool and transient connection retries. A 500-entry / 16 MB
process-local LRU cache uses snapshot ID, analyzed words, limit, and offset as its
key; original `query` is preserved even on a cache hit. Snapshot validation and
table locks precede cache lookup. Exceptions expose only controlled error text.

## 8. Build, compatibility, and scope boundaries

Search projections are prepared in phase A from the same loaded bodies as
embedding reindexing. Phase B copies unchanged rows, removes changed/deleted
rows, inserts new search data, regenerates statistics, analyzes tables, builds
HNSW and GIN, runs self-checks, registers, grants, and commits atomically.

`SEARCH_VERSION` participates in the pipeline fingerprint. A version/format
change rebuilds search data while reusing byte-identical embedding vectors.
Appendix-only edits reuse search projections; metadata-only edits update display
fields. Deletion removes the search row. The old snapshot remains served during
a build; failures leave published snapshots intact.

Self-checks cover document-ID coverage, body hashes, version, aligned normalized
and display lengths, token counts versus occurrence arrays, nonempty vectors for
nonempty token sets, corpus statistics, and a sampled indexed word lookup. Valid
short bodies need no embedding chunk. The existing malformed-body gate remains.

The agent supports snapshot formats `{1, 2, 3, 4}` for chat/readers. Current word
search requires format 4; selecting an older snapshot reports search not ready.
The backend design is complete independently of frontend integration. Full-corpus
benchmarking, deployment, and a public latency target remain operational work.

## 9. Local validation

The fixed sample contains 15 transcripts, adding seven topical documents for
local debt, Evergrande, birth rates, AI, and rail construction. Sample indexing
requires `INDEXER_SCOPE=sample` and a database name ending in `_local`.

```sh
curl -G http://127.0.0.1:18001/search \
  --data-urlencode 'q=贵州的债务问题现状及其成因'

uv run --no-sync python scripts/test_keyword_search_e2e.py \
  --report agent/eval_results/keyword_search_sample.json

# Also exercise snapshot publication, cursor continuity, retirement and restoration.
uv run --no-sync python scripts/test_keyword_search_e2e.py --lifecycle
```

The HTTP test refuses optimized Python (`-O` / `PYTHONOPTIMIZE`), so its
assertions cannot be silently disabled. Its container-side sample / `_local`
preflight uses an explicit runtime check even when the container runs optimized
Python. The HTTP test rebuilds its oracle from source projections, independently computes
SQL ranking expectations in Python, checks complete pagination, validates
highlight offsets/reader anchors, tests appendix exclusion and invalid inputs,
and checks manually selected relevant episodes for six natural-language queries.
The saved report records actual analyzed words, matched words, scores, titles,
snippets, top-result hits, and known relevant-document coverage. These sample
checks are not an exhaustive full-corpus relevance benchmark.

Local results on 2026-10-18:

| Check                                     | Result                                                                  |
| ----------------------------------------- | ----------------------------------------------------------------------- |
| Agent tests, including PostgreSQL         | 106 passed                                                              |
| Indexer tests, including PostgreSQL       | 162 passed                                                              |
| Frontend regression tests                 | 59 passed                                                               |
| Read-only localhost HTTP checks           | 46 passed                                                               |
| Local HTTP checks with snapshot lifecycle | 95 passed, including health polling                                     |
| Natural-language relevance checks         | Expected relevant episode ranked first for all six questions            |
| Corpus                                    | 15 transcripts, 79,692 body characters, 38,088 indexed word occurrences |
| Search table total size / GIN size        | 3,760,128 / 1,720,320 bytes                                             |

| Natural-language query         | First result                                        |
| ------------------------------ | --------------------------------------------------- |
| 贵州的债务问题现状及其成因     | 睡前消息581 — 信达支援贵州地方债，2023年酷似1999    |
| 地方政府城投债为什么偿还困难   | 睡前消息588 — 城投债四大网红，柳州第一              |
| 恒大房地产危机对经济有什么影响 | 睡前消息329 — 许家印的恒大，潘石屹的soho,有啥区别？ |
| 中国人口出生率下降有哪些原因   | 睡前消息461 — 日本明石没有生育奇迹，中国呢？        |
| 人工智能发展对就业会有什么影响 | 参考信息266 — 在线内容“短命”；国产AI困境            |
| 高铁建设是否会带来地方财政压力 | 参考信息515 — 财政供养人数；贵州市市通高铁          |

None of these six complete question strings occurs verbatim in the sample.
The report records actual top-three results and known-relevant coverage as well
as first-result hits; first-result success is not a claim of exhaustive recall.
Weak partial matches remain possible later in the result list.

The latest validated snapshot is `s20261018t112012z_3f23dda`, using transcript
commit `3f23dda8708cda8ddc6f485bcb3ecf6a1ebc229a`. Expanding the sample added 38
embedding chunks while reusing the existing 55. The subsequent lifecycle build
reused all 93 vectors and separately re-embedded five samples for the existing
model self-check. Search requests themselves make no model calls.

Lifecycle checks verified old-cursor continuity after publication, a 410 after
retirement, automatic fallback, and restoration of the newest snapshot. The local
report is [keyword_search_sample.json](../../agent/eval_results/keyword_search_sample.json).
Observed HTTP timings are sample diagnostics with warm buffers/cache, not a
full-corpus latency target. No frontend implementation or public search proxy is
part of this delivery. All changes remain local; no commit or push was made.


## 10. Local review corrections

Review covered the uncommitted word-search changes and commit `12eb37a` without
committing or pushing. Regressions were reproduced before their fixes:

- Cursor snapshot IDs now follow the generated ASCII identifier alphabet; an
  encoded NUL is rejected as HTTP 422 before reaching the database adapter.
- Proximity credit requires real indexed word offsets. Equal-frequency test
  documents verify that an `ai` substring inside `openai` cannot change ranking.
  Cursor version 3 prevents continuing an older ranking order across this fix.
- E2E checks refuse host Python optimization and explicitly enforce sample scope
  and a `_local` database even if container Python is optimized. URL validation
  accepts only HTTP(S) loopback targets, before any Docker operation.

The full local suite passes 336 tests (109 Agent, 168 indexer, 59 frontend).
The read-only HTTP run passes 48 checks; the lifecycle run passed 94 checks,
including variable-count snapshot polling. All six relevance checks retain their
expected first result. Formatting, lint, translated documentation structure,
and Compose validation also pass, including the documented Compose 2.24.6 floor.
The historical `stats.py` reference in the dated snapshot design is intentionally
unchanged; it documents a removed module rather than a current path.
