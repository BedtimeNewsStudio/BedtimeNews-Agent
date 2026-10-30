# Keyword Search: Precision, Postings, Hybrid Questions and Search Tools

Status: implemented and verified locally against a full-corpus copy (1,920
transcripts); not yet released. It replaces the any-word recall, the
`token_data` scan and the "no threshold" rule of the
[keyword search backend design](20261018_keyword-search-backend.md) (sections
1, 2 and 5). Snippets, anchors, the snapshot lock and the error contract of that
design still apply.

## 1. Problem

Any-word recall with no threshold returned most of the corpus for most queries,
and names the dictionary did not know split into single characters:

| Query                      | Analyzed words         | Results | Relevant                |
| -------------------------- | ---------------------- | ------: | ----------------------- |
| 阿尔忒弥斯                 | 阿尔忒弥 / 斯          |     180 | 8, then 172 matching 斯 |
| 武汉弘芯                   | 武汉 / 弘 / 芯         |     376 | 8                       |
| 钟薛高                     | 钟 / 薛 / 高           |   1,694 | 3                       |
| 贵州的债务问题现状及其成因 | 贵州 / 债务 / 问题 / … |   1,810 | far fewer               |

Questions were matched only by their words, so a transcript answering in other
words was missed. Long questions took about a second because every query word
read each candidate's whole `token_data`. There was no way to order by date,
narrow to a channel or year, recover from a misspelling, or see why a result
was listed.

## 2. Overview

```text
query ──► units (phrases, aliases, core/weak) ──► search_postings ─► gate ─► BM25 × coverage × proximity
                                                                          │
               question? ──► embedding ─► nearest chunks ──┐              ▼
                                                           ├─► RRF ─► (rerank) ─► cutoff / title / filters / order / page
                                         keyword ranking ──┘
```

| Component                         | Where                                         |
| --------------------------------- | --------------------------------------------- |
| Discovered word list (index side) | `search_words.txt`, `scripts/discover_search_words.py` |
| Snapshot format 5 with postings   | `indexer/src/snapshot_schema.py`, `builder.py` |
| Query units, aliases, questions   | `agent/src/search_query.py`, `search_aliases.txt` |
| Keyword ranking SQL, page loading | `agent/src/search_db.py`                      |
| Semantic recall, fusion, rerank   | `agent/src/search_semantic.py`                |
| Pipeline, suggestions, filters    | `agent/src/search.py`                         |
| Regression set                    | `agent/src/eval_search.py`, `eval_search_labels.json` |
| Search tools, click log           | `frontend/static/app.js`, `frontend/server.py`, `scripts/search_clicks.py` |

## 3. Index: discovered words and snapshot format 5

### 3.1 Word list (`SEARCH_VERSION = 3`)

`scripts/discover_search_words.py` reads one snapshot's bodies, segments them
with the base dictionary only (jieba's packaged one plus the domain words), and
proposes runs of 2 to 5 touching words of two kinds:

- **Names**: every touching pair includes a fragment (§4.1), the rule queries
  use. At least one single character must be rare (dictionary frequency <
  5,000) and noun-like in jieba's tags, and none may be a verb, adverb or other
  grammar word, so 很 + 难 or 赚 + 不到 is not a name. Personal names (a common
  surname first, 2 to 4 characters, dominance ≥ 0.5, entropy ≥ 1.5) skip the tag
  rule: 许家印, 王文银.
- **Compounds**: multi-character words only, where the rarest word occurs inside
  the run in at least 60% of its uses: 宁德时代, 营商环境, 总和生育率.

Both need 3+ transcripts, varied neighbouring characters on both sides
(entropy ≥ 1.0), and must not usually continue into one longer candidate.
Titles before names (总统尹锡悦), names before ordinary words (星舰项目), pieces
cut at a word boundary (局副局长) and place words at either end (微博上, 灾后) are
dropped. A reviewed block list and allow list in the script record the manual
pass. Each word gets the smallest jieba frequency that keeps it whole, so it
never captures a longer reading: 火星舰队 stays 火星 / 舰队.

The result is 1,617 words, for example 阿尔忒弥斯, 中芯国际, 钟薛高, 武汉弘芯,
海威华芯, 宁德时代, 泽连斯基, 尹锡悦, 连花清瘟, 霸王茶姬, 蜜雪冰城, 许家印. Both
images ship byte-identical copies of `search_words.txt` next to
`search_text.py`; the contract test covers both files. Search segmentation still
indexes the dictionary words inside a discovered one, so 宁德 alone still finds
宁德时代.

### 3.2 `search_postings`

Format 5 adds one table, rebuilt from `search_documents.token_data` on every
publication like the statistics (never copied from a base):

```sql
CREATE TABLE <snapshot>.search_postings (
    term TEXT PRIMARY KEY,
    document_frequency INTEGER NOT NULL CHECK (document_frequency > 0),
    postings JSONB NOT NULL          -- {doc_id: [offsets]}
);
```

A query reads only its own words' rows instead of every candidate's
`token_data`: for a five-word question, reading the postings takes about 4 ms
instead of 650 to 1,300 ms. On the full corpus the table holds 102,880 words in
70 MB and takes about 5 seconds per publication. Self-checks compare a sampled
word's postings with its transcript's `token_data` and the total of document
frequencies with the number of (transcript, word) pairs. The postings also serve
as the lexicon for suggestions (§5.4).

### 3.3 Rollout

`FORMAT_VERSION = 5` and `SEARCH_VERSION = 3` change the pipeline fingerprint,
so the first build is full; embedding vectors are reused, so it makes no model
calls. The Agent supports formats 1 to 5, prefers the highest, and serves
search only from format 5. Following the blue-green procedure for a format
upgrade: upgrade the indexer first and wait for a format-5 snapshot (old agents
keep reading format 4), then deploy the new agent.

## 4. Query analysis (`search_query.py`)

### 4.1 Units and phrases

A **fragment** is a single character with dictionary frequency below 20,000
(斯 4,120, 弘 163); more frequent single characters are words (不 360,331,
生 22,579). Touching query words where at least one of each pair is a fragment
form a **phrase** that matches only where its words sit at their query offsets:
斯 + 凯 + 迪奥 → 斯凯迪奥. A space or punctuation ends a phrase.

**Core** units decide relevance: phrases and multi-character words of full
weight. Question words (问题, 影响; weight 0.15) and lone single characters only
add score.

### 4.2 Aliases

`search_aliases.txt` lists equivalent names (特朗普 川普 trump; 俄乌战争 俄乌冲突;
国内生产总值 gdp; 推特 twitter; 人工智能 ai; 20 groups, each checked to occur in
the corpus). A unit carries every spelling as a **variant**; it matches where any
variant does and counts as one unit for idf, coverage and the gate. Touching
query words that together spell a name are merged first: 国内 + 生产总值 →
国内生产总值. Meanings that only overlap (生育率 / 出生率) are not aliases.

### 4.3 Questions

A query is a question when it contains a question marker (为什么, 如何, 哪些, 是否,
吗, ？…) or a question word such as 问题 or 影响. Questions get semantic recall
(§6); names and topics stay keyword-only, because their nearest chunks are
rarely about the name (阿尔忒弥斯 peaks at 0.45 similarity on unrelated
episodes).

## 5. Keyword ranking

### 5.1 Recall and score (`search_db.rank_documents`)

One statement reads the postings of every variant word, keeps (variant,
transcript) pairs where all words occur, verifies phrase adjacency from the
offsets, and aggregates per unit:

```text
bm25(d)  = Σ weight(u) · idf(u) · tf(u,d) · 2.2 / (tf + 1.2 · (0.25 + 0.75 · len(d) / avglen))
score(d) = bm25(d) · (0.1 + 0.9 · coverage(d)) · (1.3 if near(d) else 1)
```

`tf` sums all variants' occurrences; `df` counts transcripts with any variant.
`near(d)`: two consecutive units occur in query order within 12 characters on
one line.

### 5.2 Relevance gate

Gate units are the core units in at most half of all transcripts; if none, all
core units; if none, every unit. A transcript must match all gate units when
there are up to two, two of three or four, and half of more. When no transcript
reaches that, the requirement falls to the best coverage any transcript has.
中国, 经济 and 发展 rank but do not admit a transcript while a rarer word such as
出生率 is in the query.

### 5.3 Cutoff, title and fallback (`search.py`)

- Drop results whose body score is below 20% of the best body score.
- Multiply by `1 + 1.0 × (weight of units found in the episode title) / (total
  weight)`. Title words come from the same analyzer, cached per snapshot, so ai
  never matches inside OpenAI. Titles reorder only: they never admit a result,
  and the cutoff uses the body score.
- If nothing matches, search once more with phrases split into words, dropping
  fragments; a misspelled name returns nothing rather than every transcript with
  阿 and 尔.

### 5.4 Suggestions

When a core unit occurs nowhere, the response offers `suggestion`: the query
with that unit replaced by the closest indexed word of similar length sharing
its first or last character (at most one edit for up to four characters, two
for longer), preferring fewer edits, then a same-length substitution, then the
more frequent word. The span with an adjacent character the analyzer dropped as
a stopword is tried too. 阿尔特弥斯 → 阿尔忒弥斯, 钟雪高 → 钟薛高, 许家银 → 许家印,
泽连斯其 → 泽连斯基.

## 6. Questions: semantic recall, fusion and rerank (`search_semantic.py`)

For a question, the embedding call starts in a worker thread before keyword
ranking. The nearest 100 chunks (the chat retriever's vectors and
`match_threshold`) are grouped by transcript; transcripts within 0.08 of the
best similarity, at most 30, form the semantic list. The keyword and semantic
lists are fused by reciprocal rank (`1 / (60 + rank)` summed). Each result
reports `match`: `keyword`, `semantic` (no query word; its snippet is the
nearest chunk) or `both`. The response reports `mode: hybrid`.

An optional reranker (`rerank.model`, POST `<base_url>/rerank`) reorders the
first 10 fused results from each one's title and first 600 characters of
snippet. It is off by default: on the labelled questions it lowered MRR from
0.910 to 0.717, because a title and short excerpt do not show whether a whole
episode answers the question, and it adds 1 to 3 seconds.

Either model call may fail or time out (8 s, one retry); the search then falls
back to keyword ranking. `search_semantic: false` turns semantic recall off.

## 7. API

```http
GET /search?q=星舰&limit=20&sort=date&channel=GaoJian&year=2025&cursor=…
```

| Parameter | Contract                                                        |
| --------- | --------------------------------------------------------------- |
| `sort`    | `relevance` (default) or `date` (newest first, undated last)    |
| `channel` | A channel id such as `GaoJian`; letters, digits, `_` and `-`    |
| `year`    | 1900–2100; matches the publication date's year                 |

New response fields: `mode`, `reranked`, `suggestion`, the echoed `sort`,
`channel` and `year`, `facets` (`channels` and `years` with counts over all
results before the channel and year filters, so menus do not shrink), and per
result `match`. `total` counts results after filters. `score` orders one
response and is not comparable across modes (fused and reranked scores differ in
scale).

Each (snapshot, query) builds its result set once and caches it; order,
filters and pages slice that set, and page bodies, sections and offsets load
only for the page. Cursor version 5 binds the analyzed terms, page size, order
and filters; other cursors are rejected with 422. A question's semantic list
comes from the cache while it lasts; if it is evicted, recomputing it calls the
embedding model again, which could reorder later pages slightly.

## 8. Web interface

- Order, channel and year menus under the status line, filled from `facets`;
  the choice is in the address (`/search?q=…&sort=date&channel=…&year=…`), and a
  new query clears it.
- 「你是不是要找：…」 runs the suggestion with one click.
- A result found by meaning only says 「语义相关：未直接包含搜索词」; a keyword
  result missing some query words says 「缺少：…」.
- The status line says when a question also listed transcripts close in meaning.
- Opening a result sends an anonymous beacon to `POST /api/search/click`: query,
  transcript, rank, match, mode and snapshot. The server validates it (extra
  fields such as an address are refused) and logs it as `search_click {json}`;
  nothing is stored elsewhere. `scripts/search_clicks.py` summarizes the log:
  where clicks land in the ranking, how often semantic results are chosen, and
  the queries whose clicks land lowest.

## 9. Evaluation

`python -m src.eval_search` runs the labelled set and appends to
`agent/eval_results/search.json`; `--set module.NAME=value` tries constants
without editing code. Labels:

- 25 names and 7 multi-word queries whose truth is every transcript containing
  the strings (or all of them), computed from the snapshot so it never depends on
  the analyzer under test;
- 26 questions with labelled episodes: the 20 of `eval_retriever_labels.json`
  and the 6 of the backend design;
- 4 misspellings with their expected suggestion.

Results on the full corpus:

| Metric                         | Value  |
| ------------------------------ | ------ |
| Name and multi-word precision  | 1.000  |
| Name and multi-word recall     | 0.999  |
| Question MRR                   | 0.910  |
| Question labelled episode in top 10 | 26 / 26 |
| Suggestions                    | 4 / 4  |

Grid search over proximity (1.15 / 1.3 / 1.5), coverage floor (0.1 / 0.25),
title bonus (0.5 / 1.0 / 1.5), cutoff (0.1 / 0.2 / 0.3), semantic recall and the
reranker chose the current values. Name precision and recall barely move with
any of them. For questions, semantic recall raises MRR from 0.866 (best
keyword-only setting, where one labelled episode is not found at all) to 0.910;
title bonus 1.5 lowers it to 0.890; the reranker lowers it to 0.717.

Compared with the original any-word search (deployed snapshot, 67 queries):

| Query                          | Before | After |
| ------------------------------ | -----: | ----: |
| 阿尔忒弥斯                     |    180 |     8 |
| 武汉弘芯                       |    376 |     8 |
| 钟薛高                         |  1,694 |     3 |
| 中芯国际                       |  1,834 |    15 |
| 星舰 阿尔忒弥斯                |    341 |     4 |
| 贵州的债务问题现状及其成因     |  1,810 | 133 + semantic |
| 川普                           |    272 |   286 (with 特朗普) |
| 阿尔特弥斯 (misspelling)       |    259 |     0 + suggestion |

Latency in the Agent, including page bodies and snippets, uncached:

| Query kind                    | Median | Max    |
| ----------------------------- | -----: | -----: |
| Names (6)                     | 14 ms  | 39 ms  |
| Multi-word (4)                | 73 ms  | 337 ms (中国 经济) |
| Questions with semantic (4)   | 557 ms | 752 ms |

Before, uncached keyword search took a median 231 ms and up to 8.7 s.

Verification: agent 130, indexer 170 and web 79 tests pass, including
PostgreSQL tests for phrases, aliases, suggestions, filters, facets, cursors
bound to filters, semantic fusion and rerank with stand-in models, postings
self-checks and the word-list contract. `scripts/test_keyword_search_e2e.py`
recomputes keyword ranking independently from the source files and checks
order, filters and facets; it was not run, because the local sample stack was
down. A full-corpus copy built with the indexer's own DDL and self-check served
all measurements above.

## 10. Operations: new transcripts and when to rebuild

New transcripts need no manual rebuild. The scheduled incremental build
segments each new body with the current dictionary and writes its
`token_data`; every publication then regenerates `search_statistics` and
`search_postings` from all bodies (about 5 seconds), so new words are
searchable and appear in the suggestion lexicon at once. Only new chunks are
embedded, as before.

How a word new to the corpus is found:

| New word                                   | What happens                                               | Action            |
| ------------------------------------------ | ---------------------------------------------------------- | ----------------- |
| Known to the dictionary (低空经济)          | Indexed as one word                                        | None              |
| Unknown name split into fragments (a new company name) | The query rejoins it into a phrase that must appear contiguously | None              |
| Another spelling of a known name           | Add a line to `agent/src/search_aliases.txt`; query side only | Redeploy the agent |
| Name worth making a whole word             | Rerun `scripts/discover_search_words.py`, review, update both copies of `search_words.txt` | Rebuild (below) |

Changing `search_words.txt` changes segmentation, so the index must be rebuilt:
bump `SEARCH_VERSION`. The pipeline fingerprint changes, the next build
rebuilds all search data and reuses every embedding vector (no model calls;
about a minute on the current corpus). Until then the phrase rule already finds
those names, so the update is not urgent; it mainly lets suggestions offer the
new names and keeps segmentation tidy.

Query and index must use the same word list, so search does not simply read
the snapshot chat reads: it reads the newest published format-5 snapshot whose
registry `stats.search_version` equals the agent's `SEARCH_VERSION`
(preferring chat's snapshot when it qualifies; a `RAG_SNAPSHOT` pin still
applies, and a cursor only continues on a snapshot that still qualifies). An
agent with a new word list therefore answers "Search index is not ready" until
the indexer publishes a snapshot built with it, and never searches old
segmentation with new words; an older agent keeps reading the older lineage,
which snapshot GC keeps for 7 days after it is superseded. A word-list update
needs no `FORMAT_VERSION` bump. To avoid the not-ready window, upgrade the
indexer first, as for a format change, then the app.

## 11. Limits and next steps

- The word list is reviewed by hand once; new names in later episodes are
  rejoined as phrases at query time until the next review run of the script.
- Labels cover 63 queries; clicks from `/api/search/click` should add questions
  whose clicks land low.
- A cross-encoder reranker could help with longer passages (several chunks per
  episode) but costs more time; the current one is measured and left off.
- Facets count channels and years only.
