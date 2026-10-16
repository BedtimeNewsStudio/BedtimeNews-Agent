# Section Citations: Anchors on Body `##` Topics

Status: implemented. The start gate in section 2 passed before the code change. A full format 2 snapshot of the corpus was not published. Local verification used the sample scope (`indexer/index_config.sample.yml`, which now also includes `CanKaoXinXi/0001-0100/0017.md` so a body `##` title with an internal space is indexed) and published `s20261015t111135z_6d50919` (format 2, 48 chunks, 7 transcripts). The local agent selected it. `SUPPORTED_FORMATS` stays `{1, 2}`.

The sample cleanup in section 11 was run on 2026-10-15. `drop-previous-lineage` removed the format 1 snapshot `s20261015t055140z_0c6843d`. A later build of the same lineage published `s20261015t120010z_c618f15`, which is the snapshot the sample agent reads. The superseded format 2 snapshot `s20261015t111135z_6d50919` was then dropped from the sample database. Production does not hand-delete that previous current-lineage snapshot; ordinary GC keeps the two newest.

Citations shipped as flat sources. A topic chunk's id is `URI#anchor`, composed at retrieval from `doc_id` and `heading_anchor`. The model does not write the standardised title or the URL. Repair does. If the model writes only the episode name, the link still opens a retrieved section, not the start of the transcript. The local sample agent at `http://127.0.0.1:8080/` is running this rule.

"Snapshot", "pipeline fingerprint", "format version", "vector reuse", and the agent's selection rule mean the behavior already shipped in the indexer and the agent.

| Constant                                                           | Before | After                                                                                       |
| ------------------------------------------------------------------ | ------ | ------------------------------------------------------------------------------------------- |
| `indexer/src/chunker.py` `CHUNKER_VERSION`                         | 1      | 2                                                                                           |
| `indexer/src/document_loader.py` `BODY_NORMALIZATION_VERSION`      | 3      | Stays 3. Body extraction does not change                                                    |
| `indexer/src/snapshot_schema.py` `FORMAT_VERSION`                  | 1      | 2                                                                                           |
| `agent/src/snapshots.py` `SUPPORTED_FORMATS`                       | `{1}`  | `{1, 2}`. Do not narrow a running agent to format 2 before a format 2 snapshot is published |
| `indexer/src/transcript_export.py` `TRANSCRIPT_PROJECTION_VERSION` | 4      | 5, heading `id`s are written on body topic `h2`s                                            |

`document_chunks.heading` already existed, and the agent's retrieval SQL already selected `c.heading`. This change adds `heading_anchor`, per-section citation repair, and reader scrolling. Overlap already stayed inside a heading.

## 2. Start gate

Passed on 2026-10-15, before the indexer, agent, and frontend changes. The start request was explicit, and a rescan of the checkout the indexer reads (`indexer/data/BedtimeNews-Transcripts` at `6d509196`, the same commit as the transcripts repository) found no violations: 1907 transcripts, `INDEX.md` skipped. A file list from indexer checkout `0c6843d` is historical only and was not the tree that was built.

The contract that rescan checked, and that each build still checks, looks only between `## 正文` and `## 附录`, not including those two lines:

- No line that starts with `#` and does not start with `##`. A greeting is a paragraph.
- No `###` or deeper heading. `### 事实订正`, `已核对`, and `待核对` live in the appendix, outside this span, and stay.
- No `[^`. The appendix also has no `[^n]:` definitions left.
- Every `##` title is short plain text: no `](`, no `[^`, no wrapping `**` or `__`. The corpus gate also required titles to be unique in the file. A repeat is disambiguated, not rejected.
- Exactly one `## 正文` and one `## 附录`, with the body first.

There is no indexer special case for a leftover `#`, `###`, or footnote, and the old fallback that treated every heading level as a cut is gone.

Each build still checks a violating file on its own. A `#` heading, a `###` or deeper heading, a `[^` marker, a topic title that still contains `](` or `[^`, or a topic title wrapped in `**` or `__`, is logged with its URI and skipped (zero chunks). The run continues. `extract_body` already warns and returns an empty body when `## 正文` is missing. Identical topic titles are not a reject: their anchors are `title`, `title-2`, `title-3`, in order, and the reader `h2` ids use the same strings.

## 3. Goal

The citation label stays the standardised title, for example 睡前消息123. The link lands on one topic inside that episode's body:

`[[睡前消息123]](/transcripts/ShuiQianXiaoXi/0101-0200/0123.md#话题标题)`

Opening that URL directly, and clicking it inside the app, both scroll the reading pane to the matching `##`. Two topics from the same episode are two links: the same label, different fragments. A citation with no topic anchor has no hash and opens the start of the transcript.

## 4. Sections

Only the text between `## 正文` and `## 附录` is indexed. `extract_body` already drops the body marker line, the appendix, and everything after it. That stays.

After cleanup, every citable topic in that span is one sibling `##`. A section runs from that `##` to the next `##`, or to the end of the body.

- Text before the first `##` is the preamble: `heading` empty, `heading_anchor` empty, citation with no hash.
- A single-topic episode that has no inner `##` is entirely preamble. Do not invent a heading.
- A `###` is not a section. Appendix headings are not sections.
- The greeting 「大家好」 is not a heading.
- Every chunk cut from one `##` shares one anchor. The anchor names the heading, not the chunk's offset inside the section.
- `---` is a horizontal rule, not a heading.

`document_chunks.heading` stores the heading's plain text, without the `##`. The preamble's `heading` stays empty (`None` today).

## 5. Anchors

One function in the indexer, called by both the chunker and `transcript_export`. Do not implement it twice, and do not reimplement it in the frontend. The frontend does not recompute anchors: the `id` is stored in `body_html`, and the browser decodes the fragment and looks it up.

The input is the heading's plain text. The output is the anchor string shared by `heading_anchor`, the HTML `id`, and the URL fragment.

1. Strip a leading `##` and the whitespace after it.
2. If a Markdown link remains, unwrap it to its link text and drop the URL, repeatedly. Cleanup should have removed these. This step is defensive.
3. Delete a leftover `[^...]`.
4. Strip a wrapping pair of `**`, `__`, or backticks.
5. Collapse each run of whitespace into one `-`, then strip leading and trailing `-`.
6. An empty result is no anchor: a whole-document link, and no `id`.
7. Within one document, in order, deduplicate. If the string is already used, try `-2`, `-3`, and so on until it is free. The preamble does not take part.

The database value and the HTML `id` are this raw string, not percent-encoded. Only the fragment is encoded. An anchor contains no `/`.

The reader writes an `id` only on topic `##` headings inside the body, in the same order the chunker sees those topics, so the nth duplicate is `-2` on both sides. Do not write a topic `id` on `## 正文`, `## 附录`, an appendix `###`, or the H1.

## 6. Chunking

Parameters stay: target about 1000 words, maximum 2500, overlap 150 inside the section, drop a chunk under 50 words. Overlap stays inside one section. `chunk_document` in `chunker.py` already does this.

The boundary changes. Inside the body, only a heading of exactly two `#` characters (`##`, not `###`) is a cut. Today's `HEADING_PATTERN` is `#{1,6}`, and the level is discarded. After cleanup the body should contain no other level. If the build-time check finds one, reject that file under the start-gate rule. Do not treat the `#` or `###` as a section.

Chunk text still starts at the heading line, includes the heading, and runs to the cut inside the section. An edit in one section therefore does not change any other section's chunk text.

Raise `CHUNKER_VERSION` to 2. The pipeline fingerprint includes the chunker version, so the next build is a full snapshot and cannot incremental-copy from format 1. Byte-identical chunk text still reuses its vector through the existing `text_hash`. A transcript that was already flat `##` can keep the same chunk text and skip the embedding API. A transcript whose body the cleanup changed already has a new `body_hash` and would be rechunked anyway.

`BODY_NORMALIZATION_VERSION` stays 3.

## 7. No section hashes

Do not add a hash per `##` section, a section-level diff, or "re-embed only the sections that changed".

Keep the two gates that already exist:

- Whole-file `source_hash`: an unchanged file is not parsed.
- `body_hash` of the normalized body: a title, date, or appendix-only edit takes the `source_only` path, re-renders reader HTML, and does not re-embed. Only a body change rechunks that file.

Vector reuse stays the SHA-256 of each chunk's text (`text_hash` in `builder.py`; on insert, the embedding is joined back from the base snapshot on `sha256(convert_to(text, 'UTF8'))`). Overlap does not cross a heading, and the chunk text includes the heading line, so an unchanged section does not call the embedding API after some other section is edited.

Keying a section hash by heading position is worse. Inserting a `##` in the middle changes the position key of every later section and re-embeds text that did not change. The expensive part of a publish is copying the whole snapshot and rebuilding HNSW. A section hash does not avoid that. Incremental Phase B still copies the base, deletes that `doc_id`'s chunks, inserts the whole document again (vectors joined by text hash), and rebuilds HNSW.

## 8. Snapshot format 2

The agent reads the new column, so `FORMAT_VERSION = 2`. A column the agent does not read does not need a format bump. This one does.

Add `heading_anchor TEXT NOT NULL DEFAULT ''` to `document_chunks`. The empty string means no fragment. `COPY_COLUMNS["document_chunks"]` lists `heading_anchor` immediately after `heading`. Format 1 has no such column.

During the transition, `SUPPORTED_FORMATS = {1, 2}`. The selection rule is unchanged: the highest supported `format_version`, then the newest published snapshot in that embedding space. Therefore:

- Before a format 2 snapshot is published, an agent that supports 1 and 2 keeps reading format 1, and citations stay document-level.
- After format 2 is published, that agent switches to it on the next selection.
- An old agent that only supports `{1}` keeps reading format 1 until that agent is upgraded. Do not narrow a running agent to format 2 before the snapshot exists. It would have nothing it can read.

Retrieval SQL does not `SELECT heading_anchor` on format 1. It does on format 2. Format 1 citations stay document-level. On format 2 an empty anchor is the preamble and stays a document URL. A chunk with an anchor is the source `URI#anchor`.

The pipeline fingerprint includes `format_version`. Format 2 cannot incremental-copy from format 1. The first format 2 snapshot is a full build. Vectors of identical text are still reused from the older snapshot by text hash.

## 9. Citations

A section is a source in its own right, parallel to a whole episode. The indexer already stores `heading_anchor` on the chunk. Retrieval composes the source id from that column. It is not a nested field the model has to fill in.

- A topic chunk's source id is `<URI>#<anchor>`, for example `CanKaoXinXi/0401-0500/0490.md#好莱坞开除工贼`.
- A preamble chunk, or a chunk with an empty anchor, keeps `<URI>`.
- `<URI>` stays `doc_id`, the path relative to `contents/` including `.md`. The database column does not gain a `#`. Titles, the reader route, and incremental builds stay one row per episode.
- `<anchor>` is the raw string. Only the URL fragment is percent-encoded.

The model does not write the standardised title or the URL. Both are uncertain if they are left to generation, and both are a lookup. Each passage has one `Source:` line with that id. There is no second list of topic names for the model to copy. When it attributes a claim, it copies the source id between double square brackets. Title and link are added afterwards. A single pair of backticks around that token is removed during repair; left in place, the link renders as code and the reader sees the raw `[[title]](url)`.

Repair produces what the reader sees. The link text is the standardised title, plus ` · ` and the section name when the link opens a section (decided 2026-10-16, replacing the earlier title-only label: a reply that cites several sections of one episode showed identical chips). The section name is the heading as plain text with Markdown, footnotes and ASCII square brackets removed, at most 12 characters, cut to 11 plus one `…` when longer; the full heading appears as a tooltip. A whole-document link shows the title alone. Adjacent citations that resolve to the same link collapse to one. Each citation run then sits inside the sentence it supports, in full-width parentheses before the final punctuation (decided 2026-10-16): `_place_citation_runs` on the server and `placeCitationRuns` in the frontend apply the same rule, leave a source-list item and a run already inside a parenthetical alone, and are idempotent. Before placing, `_collapse_repeated_citations` (and the frontend's `collapseRepeatedCitations`) keeps a run of sentences from one source cited once, at the last of them: within a paragraph, the same run repeated with no different citation in between is dropped at every occurrence but the last (decided 2026-10-16). Alternating sources, other paragraphs, source-list lines and runs inside a parenthetical are not collapsed. The streaming `citations` map carries `title` and `label`. The link is `/transcripts/<URI>` or `/transcripts/<URI>#<percent-encoded anchor>`.

`[[睡前消息123]](/transcripts/<percent-encoded URI>#<percent-encoded anchor>)`

Keys:

- `<URI>#<anchor>` and `<standardised title>#<anchor>`: that section, only when this retrieval returned the pair.
- The bare `<URI>` and the bare standardised title: when this retrieval has any section of that episode, the strongest section by similarity, not the start of the transcript. Passage repair may replace it with another retrieved section of the same episode when the text before the citation matches that heading. When the text matches none, the link still opens a retrieved section. An episode with no topic anchor stays a whole-document link.
- An invented `#anchor` falls back to that episode's retrieved section, not to the article start.
- A `<URI>` written without its `.md` suffix, with or without `#anchor`, resolves as if the suffix were there. The model often drops it when it copies a source id. Both the server (`_lookup_citation`) and the frontend (`linkifyCitations`) add it before a lookup fails.

Streaming `citation_urls` uses the same keys. The bare title therefore already carries a section URL while the answer is streaming. Server-side repair still runs after generation and can choose a different retrieved section per occurrence. The frontend `CITATION_RE` accepts a `#` inside `[[...]]`. Do not cut the token at `#`. A citation that is already `[[标题]](url#fragment)` is left as written: looking the bare title up again would replace that fragment.

`《standardised title》`, if kept, follows the same bare-title rule. An unknown title such as `《三体》` stays prose.

## 10. Reader

The four failures below are the pre-change diagnosis. The "Change it to" list is what shipped.

A section cannot be opened today, for four concrete reasons:

- markdown-it in commonmark mode emits no heading `id`s.
- The bleach allow-list permits `id` on `a`, `li`, `ol`, `p`, `section`, `span`, and `sup`, not on `h2`–`h6`. An `id` written on a heading is stripped.
- `_normalize_relative_uri` drops a fragment on a relative link with `href.split("#", 1)[0]`. A bare `#...` footnote link is already kept.
- `navigate()` compares and writes history from `pathname + search` only. When the path and search are unchanged it returns immediately, and the hash is lost. `showReader` scrolls `#reading-scroll` to the top, then sets `innerHTML`, and never reads the hash. In-pane footnotes use `scrollReadingPaneTo` because the shell is `100dvh`: `scrollIntoView` and the browser's own hash scroll also move the outer page and leave a blank band.

Change it to:

- A topic `h2`'s `id` is the raw anchor. Allow `id` on `h2`–`h6` in bleach. Topics land only on `h2`. The wider allow-list is so an `id` is not stripped by mistake.
- When a relative in-article link is rewritten to `/transcripts/<URI>`, keep a fragment that was already there, encoded with the same rules. Do not invent a fragment for a cross-episode link that had none.
- `navigate` writes the hash into history. If the path and search are unchanged and only the hash changed, do not refetch. Scroll the open article with `scrollReadingPaneTo` to the id after `decodeURIComponent`. Clicking the link whose hash is already in the address bar scrolls again and does not push another history entry. If the path changed, load as today, then scroll after `innerHTML` is set. If the reading pane is still sliding open, wait until its `flex-basis` transition ends before measuring. A measurement during that slide uses the narrow column and lands past the heading. A missing id or an empty hash stays at the top.
- A direct open of `/transcripts/<URI>#<anchor>` does the same. The hash never reaches the server. SSR returns the whole `body_html`, and the scroll happens on the client after that HTML is placed in the reading pane. Do not rely on the browser's native hash scroll.
- Existing in-pane `#` footnote clicks keep using `scrollReadingPaneTo`.

Raise `TRANSCRIPT_PROJECTION_VERSION` to 5. A projection-version change re-renders HTML and does not re-embed. This step can ship before format 2. Citations are still document-level, but a hand-opened URL with an anchor already scrolls to the heading.

## 11. Publish order

1. Shared anchor function, `id`s on body topic `h2`s, fragments kept on rewritten links, and reading-pane scroll by hash. Citations stay document-level. Projection version 5, re-render, no re-embed. The reader can ship on its own. In the same change, update the step 1 items under Documentation and Tests.
2. The chunker writes `heading` and `heading_anchor`. Citation repair and the streaming map become per section. The agent accepts formats 1 and 2. Format 1 has no anchor column, so its citations stay document-level. This code can merge before a format 2 snapshot exists; until that snapshot is published, live behavior does not change. In the same change, update the step 2 items under Documentation and Tests. The docs must say that a format 1 snapshot's citations have no hash.
3. Reindex in production, then delete the old chunking. The fingerprint change forces a full build of the corpus. Do not start a second embedding job. Unchanged chunk text reuses its vector from the format 1 snapshot, so that snapshot has to still be there while the build runs. Dropping it first throws away the vectors the new snapshot would have copied.

   On the host that runs the indexer, after the new indexer image is the one in the container:

   1. Recreate only the indexer. Its startup run is the reindex. Wait until `python -m src.snapshots list` shows a published snapshot whose lineage is `current` and whose format is 2. Do not judge this by a fixed number of minutes.
   2. Upgrade the agent so `SUPPORTED_FORMATS` is `{1, 2}`. Do not narrow it to `{2}`. An agent that only supports `{1}` would then have nothing to read, and this step does not do that.
   3. Confirm the live agent's `/health` names that format 2 snapshot. Until it does, do not delete anything.
   4. `python -m src.snapshots drop-previous-lineage --dry-run`. It must name only the old chunking in this embedding space: format 1, `legacy`, and retired rows of that lineage. It must not name the current lineage.
   5. `python -m src.snapshots drop-previous-lineage`. This drops those schemas and registry rows. The copied vectors already live in the format 2 snapshot.
   6. `list` again. Only the current lineage remains. The agent's `/health` still names the same format 2 snapshot. Run the command once more; it prints `nothing to drop`.

   The command refuses, and drops nothing, when no current-lineage snapshot is published or an old snapshot is pinned. A different embedding space is a different model and is not this cleanup. Ordinary GC still keeps the two newest snapshots of the current lineage. This command does not delete that previous format 2 snapshot.

## 12. Tests

Some existing assertions say "every heading level is a section" and "one document has one link". Change those assertions. Do not keep the old behavior so the old assertions stay green. Cover new behavior that has no test yet, in the same change as the code.

### 12.1 Step 1

`indexer/tests/test_transcript_export.py`

- `test_projection_includes_preamble_body_and_appendix`: the `h2` for `## 第一节` has `id="第一节"`. `## 附录` and `### 事实订正` have no topic `id`.
- `test_projection_sanitizes_html_and_suppresses_remote_images`: a topic `h2`'s `id` survives cleaning. Scripts, `onclick`, and remote images are still removed.
- `test_projection_localizes_internal_and_old_site_links`: keep the assertions for a relative link and an old-site link that have no fragment. Add a relative link that has a `#`, and assert the rewritten URL still has the fragment, encoded the same way as the anchor function.
- Keep `test_footnotes_are_bidirectional_links`, `test_projection_rejects_absolute_encoded_and_backslash_paths`, and `test_projection_handles_missing_title_and_date`. A footnote's own `id` is not a topic anchor.

`frontend/tests/test_server.py`

- `test_transcript_page_contains_raw_ssr_body_and_safe_metadata`: `body_html` is placed in the page as stored. Use a body that contains `<h2 id="话题">` and assert that `id` is still there. The canonical link has no hash, because the hash never reaches the server.

There is no `app.js` unit test, and this design does not add a frontend test framework for one. For step 1, walk the reader in a browser: open a URL that already has a hash, follow an in-app link to another transcript's hash, change only the hash on the same transcript and confirm there is no refetch, and confirm a missing id stays at the top. Do this at a desktop width and a narrow width. The thing that scrolls is the reading pane, not the page shell.

### 12.2 Step 2

`indexer/tests/test_chunker.py`

- `TestExtractHeadings.test_levels_and_text`, `test_positions_are_increasing`, `TestSplitIntoSections.test_records_heading_per_section`, and `test_section_content_spans_to_next_heading` currently require `#` and `###` to be sections. Change them so only `##` is a boundary. A `#` or `###` in the body yields zero chunks for that file and an error log. Do not make the test pass by ignoring those lines and still cutting at the neighboring `##`.
- `test_overlap_never_crosses_a_section_heading`, `test_preamble_before_first_heading_is_chunked`, `test_overlap_is_kept_within_a_section`, and `test_text_before_first_heading_is_kept` already use `##`. Keep the split and overlap assertions. Add `heading_anchor`: empty for the preamble, the heading text for a topic.
- Keep the tests for the 50-word minimum, chunk ids, and contiguous indexes.

Give the anchor function its own tests, in its own test module. The chunker and export tests only assert that they used the same result. Cover: whitespace collapsed to one `-`; a link unwrapped; `[^...]` removed; a wrapping `**` stripped; the second identical heading in one file becoming `-2`; the HTML `id` being the raw string and only the URL fragment being encoded. For one document, the chunker's `heading_anchor` and the exported `h2 id` match.

`agent/tests/test_graph_smoke.py`

- `test_citation_map_is_keyed_by_uri_and_title`: two chunks with the same `doc_id` and different anchors put `URI#anchor` on the two sections. The key is the standardised title, and the link text adds the section name (`test_section_label_is_plain_and_at_most_twelve_characters` and `test_chip_label_adds_the_section_only_when_the_link_has_one` pin the label rules; `test_adjacent_duplicate_citations_collapse_to_one` pins the run dedupe). The bare URI and the bare title open the strongest section, not the start of the transcript.
- `test_citation_repair_covers_every_spelling`: `[[URI#anchor]]`, `[[title#anchor]]`, the bare URI (with or without `.md`), the bare title, and `《title》` all open that section when the retrieval has one. An invented anchor opens a retrieved section of that episode, not the article start. An unknown `《三体》` stays prose.
- `test_bare_title_opens_the_heading_the_paragraph_is_about`: when several sections were retrieved, the text before a bare title picks the matching heading. When the text matches none, the link still opens the strongest retrieved section.
- `test_heading_text_is_not_a_citation_key`: the bare heading is not a citation key. The source id is `URI#anchor`. A preamble's source id is the URI.
- `test_citations_event_carries_titles_and_urls` and `test_uri_citations_are_rewritten_into_titled_links`: a chunk with `heading_anchor` makes the bare URI and the bare title carry that section URL. The label stays the standardised title. An empty anchor is a whole-document link.
- The 「大家好……」 heading on `_fake_chunk` can stay. It means the retrieved heading carries no useful topic. It is not a greeting `#`.
- When `ChunkResult` gains `heading_anchor`, default it to the empty string so existing constructors do not all have to set it.

`agent/tests/test_snapshots.py`

- `test_higher_supported_format_beats_newer` already sets the supported set to `{1, 2}` for that test. After production code uses that set, the assertion stays "a higher supported format beats a newer lower format". Do not change it so that only format 2 counts.
- The fake row in `test_retrieval_cache_is_per_snapshot` has only `heading`. If results carry `heading_anchor`, put an empty string on the fake row. The cache stays per snapshot.
- This file has no test that format 1 SQL does not select `heading_anchor` and format 2 SQL does. Add one.

`indexer/tests/test_pipeline.py`

- Do not add `heading_anchor` to `LEGACY_DDL` or `_populate_legacy`. `test_adoption_registers_legacy_and_first_build_reuses_every_vector` must stay green: the old `rag` schema has no such column, and the first build after adoption still reuses vectors by chunk-text hash. If new code assumes the base table always has the column, fix the code. Do not patch this old DDL.
- Keep `test_chunker_bump_forces_full_build_reusing_vectors`. Format 2 likewise changes the fingerprint and cannot use format 1 as an incremental base. Identical text still reuses its vector. `COPY_COLUMNS["document_chunks"]` includes `heading_anchor`.

### 12.3 Leave unchanged

- `indexer/tests/test_document_loader.py`: `extract_body` still keeps `##` lines inside the body and still drops `## 正文` and the appendix. Rejecting a bad heading belongs in chunking or the build, not in extraction.
- `indexer/tests/test_change_detector.py`: the gates stay whole-file `source_hash` and body `body_hash`. Do not add a section-hash test.
- `indexer/tests/test_catalog.py`: `pipeline_fingerprint(2, 3, 1)` and chunker version 2 already assert a fingerprint different from today's. Keep those expectations. The previous-lineage tests in this file cover the cleanup: same embedding space only, retired rows included, no drop without a published current snapshot, and a pinned old snapshot blocks the whole drop.
- `agent/tests/test_cache.py`: the cache key still includes `include_heading`. Do not add a dimension for the anchor.
- `agent/tests/test_frontend_contract.py`: it only checks SSE event names. The `citations` type does not change.
- `agent/tests/test_grading.py` and `agent/tests/test_transcripts_api.py`: they do not assert citation URLs or heading `id`s.

## 13. Documentation

Update the technical docs. Do not change the boxes or edges of the four structure diagrams, and do not redraw them for anchors. The repository keeps the Chinese, English, and Spanish READMEs on the same facts. A change to one language updates the other two in the same change.

### 13.1 Diagrams: no change

The diagrams are hand-written SVG files with no separate source. The pipeline, the request path, and the nodes are unchanged. There is no new phase, no new service, and no new workflow node.

| Diagram                                   | READMEs that include it                  | Why it stays                                                                                                                                         |
| ----------------------------------------- | ---------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `docs/diagrams/indexer-pipeline.svg`      | `indexer/README` in all three languages  | "Chunk 正文", reuse by `sha256(chunk text)`, and "Markdown → HTML" remain true. An anchor is a property of chunking and of rendering, not a new step |
| `docs/diagrams/system-architecture.svg`   | Root README in all three languages       | Still browser → web → agent reading `rag_s<id>`, with the indexer publishing snapshots                                                               |
| `docs/diagrams/frontend-architecture.svg` | `frontend/README` in all three languages | Hash scrolling happens inside the static page. Requests are still HTTP :8080 and SSE                                                                 |
| `docs/diagrams/agent-workflow.svg`        | `agent/README` in all three languages    | Still Retrieve → Grade → Generate. Citation repair stays after generation and is not its own node                                                    |

Do not edit these files' `<title>` or `<desc>`. Do not add a "current snapshot" line to `agent-workflow.svg`.

### 13.2 Living docs in step 1

Update these when the reader scrolls by hash and citations are still document-level. Do not add `#` to the example URLs yet.

`frontend/README.md`, `frontend/README.en.md`, `frontend/README.es-ES.md`

- Under Features, add that opening `/transcripts/<URI>#<anchor>` directly, and following an in-app link that only changes the hash of the open transcript, both scroll the reading pane to that heading. A missing id stays at the top. Scrolling uses the reading pane, not the browser's native hash scroll.
- The Design sentence about not rendering separator rules for headings, `h2`, and footnotes is about the rules. The headings themselves stay. Do not rewrite it so that `h2` elements are hidden. That would contradict the anchors.
- Leave the `citations` example in the API contract as a whole-document URL until step 2.

`indexer/README.md`, `indexer/README.en.md`, `indexer/README.es-ES.md`

- On `transcripts.body_html`, add that a body topic `h2` carries an `id` equal to the anchor. A projection-version change re-renders and does not re-embed.
- Leave the smart-chunking bullet describing today's behavior. Step 2 narrows it to sibling `##` only.

### 13.3 Living docs in step 2

Update these in the same change as the chunker, `heading_anchor`, citation repair, and `SUPPORTED_FORMATS = {1, 2}`. If no format 2 snapshot is published yet, the docs say the agent still selects format 1, that snapshot has no `heading_anchor`, and its citations have no hash.

`indexer/README` in all three languages

- Smart chunking: one sibling `##` inside the body is one section; text before the first `##` is the preamble; overlap does not cross a section; a chunk under 50 words is dropped. A `#` or `###` still present in the body rejects that file. Do not keep the wording "one section per sub-heading", which also counts `#` and `###`.
- In the `document_chunks` column list, add `heading_anchor` after `heading`. The empty string means no fragment. Say this column exists on format 2 and not on format 1.
- Do not rewrite the existing fingerprint rules, or the rule that raising `FORMAT_VERSION` forces a full build while vectors are still reused by chunk text. Do not document a section hash here.

`agent/README` in all three languages

- In the snapshot-selection paragraph, change `SUPPORTED_FORMATS` from "currently `{1}`" to `{1, 2}`. Only format 2 has section anchors. A process that only supports `{1}` keeps reading format 1.
- Under "Where citation titles come from": a topic chunk's source id is `URI#anchor`, composed at retrieval. The model does not write the standardised title or the URL. Repair sets the label to the standardised title and puts the anchor only in the link. Two topics in one episode are two sources. A bare episode name still opens a retrieved section: the heading matched by the preceding text, otherwise the strongest section. A preamble with no topic anchor stays a whole-document link.
- In the non-streaming `answer` example and the streaming `citations` example, show one section link that has a `#` and keep one whole-document link that has none, so the examples are not read as "every citation has an anchor". `citations.urls` is keyed by the source id `URI#anchor`. The bare URI and the bare title of an episode that has a retrieved section use that section's URL, and the `title` field stays the standardised title.

`frontend/README` in all three languages

- Align the `citations` example in the API contract with the agent's streaming example: a key includes `URI#anchor`, and `url` carries the encoded fragment.

Root `README.md`, `README.en.md`, `README.es-ES.md`

- Keep the indexing-scope sentence: only `## 正文` is indexed; the appendix and the publication-date line are not. After it, add that the citation label is the standardised title and the link opens the matching `##` in the body. With no topic anchor, the link opens the start of the transcript.

### 13.4 Docs that stay as they are

- `docs/designs/20261008_blue-green-deployment.md`. Deploy topology and switch order do not change. Format 2 is still "after the new snapshot is published, the new instance selects it".
- `storage/postgres/migrations/README.md`. It already says a change to a served table is a new `format_version`, and that no further migrations of that kind will be added. Do not add a SQL migration for `heading_anchor`.
- The transcript checkout under `indexer/data/` and its README. That is a working copy of the upstream repository, not a document this design edits.

## 14. Out of scope

- The start gate passed before these changes. Do not add indexer special cases for a leftover `#`, `###`, or footnote.
- Do not rewrite spoken wording, the H1, URIs, `URI映射.md`, or `INDEX.md`.
- No section-hash table and no section-level diff.
- Do not redo the answer copy button, which is already shipped.
- Do not redraw the four SVGs under `docs/diagrams/`.
