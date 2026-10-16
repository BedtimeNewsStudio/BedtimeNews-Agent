"""End-to-end smoke test for the streaming contract.

The agent depends on LangChain and LangGraph behaviour that is not public API:

  * ``astream_events(version="v2")`` must emit ``on_chat_model_stream`` while a
    node is running a *blocking* ``.invoke()`` — that is the only reason tokens
    reach the browser at all.
  * every event must carry ``metadata.langgraph_node``, which is what routes
    reasoning steps, the citation map and the final answer to the right place.

Neither is covered by a type checker or by the linters, and a dependency bump
can change either without anything going red. This test drives the real compiled
graph with a stubbed chat model, so a regression in that contract fails here
instead of silently shipping an agent that no longer streams.

No credentials, no network, no database: the chat models, the retriever and the
chunk-text fetch are all replaced.
"""

import asyncio
from urllib.parse import quote

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from src import agent as agent_mod
from src import graph as graph_mod
from src.models import ChunkResult, RetrieveResponse
from src.snapshots import Snapshot, snapshot_manager

# What the model is instructed to write: the document's URI, nothing else. The
# titled link the reader sees is produced by the repair pass, not by the model.
CITED_URI = "CanKaoXinXi/0401-0500/0490.md"
ANSWER = f"鹤岗是资源枯竭型城市的代表 [[{CITED_URI}]]。"
CITED_URL = "/transcripts/CanKaoXinXi/0401-0500/0490.md"
FOLLOWUPS = ["鹤岗的房价现在怎么样？", "还有哪些收缩型城市？"]

# What the generation model returns: the answer, the delimiter, then suggestions.
GENERATION_OUTPUT = f"{ANSWER}\n\n{graph_mod.FOLLOWUPS_DELIMITER}\n" + "\n".join(
    FOLLOWUPS
)


def _fake_chunk(i: int) -> ChunkResult:
    return ChunkResult(
        chunk_id=f"chunk-{i}",
        doc_id=f"CanKaoXinXi/0401-0500/{490 + i:04d}.md",
        title=f"参考信息{490 + i}",
        chunk_index=i,
        heading="大家好，欢迎收看睡前消息",  # deliberately uninformative, as in the corpus
        # Empty anchor: the greeting heading is not a topic, so the link is the
        # whole document. Chunk 0 carries a real section so the event map has
        # both shapes.
        heading_anchor="鹤岗" if i == 0 else "",
        text=None,
        word_count=800,
        similarity=0.7 - i * 0.01,
        rank=i + 1,
    )


class _FakeRetriever:
    def retrieve_batch(self, requests, snapshot):
        assert snapshot.snapshot_id == "s1", "retrieval must read the pinned snapshot"
        return [
            RetrieveResponse(
                query=r.query,
                match_threshold=r.match_threshold,
                match_count=r.match_count,
                results=[_fake_chunk(i) for i in range(3)],
            )
            for r in requests
        ]


@pytest.fixture(autouse=True)
def pinned_snapshot():
    snapshot_manager.set_for_tests(Snapshot("s1", "rag_s1", 1, "space"))
    yield
    snapshot_manager.set_for_tests(None, "reset")


@pytest.fixture
def stub_pipeline(monkeypatch):
    """Replace every outbound dependency of the graph."""
    # route -> RAG, query_rewrite -> one query, grade -> all three relevant.
    fast = GenericFakeChatModel(messages=iter(["RAG", "鹤岗 收缩型城市", "1,2,3"]))
    generation = GenericFakeChatModel(messages=iter([GENERATION_OUTPUT]))

    monkeypatch.setattr(graph_mod, "_fast_llm", fast)
    monkeypatch.setattr(graph_mod, "_generation_llm", generation)
    monkeypatch.setattr(graph_mod, "retriever", _FakeRetriever())
    monkeypatch.setattr(
        graph_mod,
        "fetch_chunk_texts",
        lambda _schema, ids: {i: f"关于鹤岗的正文内容 {i}" for i in ids},
    )


def _collect(question, history=None):
    async def run():
        return [e async for e in agent_mod.agent_stream_query(question, history)]

    return asyncio.run(run())


def test_streaming_turn_emits_the_documented_event_sequence(stub_pipeline):
    events = _collect("鹤岗为什么成了收缩型城市的代表？")
    kinds = [e["type"] for e in events]

    # The token stream is the contract most likely to break on an upgrade: it
    # only exists because astream_events surfaces on_chat_model_stream from a
    # blocking .invoke() inside the generate node.
    assert "answer_chunk" in kinds, (
        "no answer_chunk events — astream_events stopped surfacing "
        "on_chat_model_stream, so the UI would show nothing while generating"
    )

    streamed = "".join(e["content"] for e in events if e["type"] == "answer_chunk")
    assert "鹤岗" in streamed

    # Steps depend on metadata.langgraph_node being present and matching the
    # node names in _NODE_STEP_TYPES.
    steps = {e["step"] for e in events if e["type"] == "step"}
    assert {"route", "rewrite", "retrieve", "grade"} <= steps, (
        f"missing pipeline steps, got {steps} — langgraph_node metadata or the "
        "node names changed"
    )

    # Grading kept documents, so the citation map must reach the client before
    # the answer does; otherwise citations stay dead text for the whole answer.
    citations = [e for e in events if e["type"] == "citations"]
    assert citations, "no citations event — client cannot linkify while streaming"
    assert kinds.index("citations") < kinds.index("answer_chunk")

    # Exactly one terminal event carrying groundedness.
    terminal = [e for e in events if e["type"] in ("answer_final", "answer_meta")]
    assert len(terminal) == 1, f"expected one terminal event, got {terminal}"
    assert terminal[0]["grounded"] is True


def test_followups_are_split_out_of_the_answer(stub_pipeline):
    events = _collect("鹤岗为什么成了收缩型城市的代表？")

    followups = [e for e in events if e["type"] == "followups"]
    assert followups, "no followups event"
    assert followups[0]["items"] == FOLLOWUPS

    # The delimiter must never survive into the answer the client renders.
    terminal = next(e for e in events if e["type"] in ("answer_final", "answer_meta"))
    if terminal["type"] == "answer_final":
        assert graph_mod.FOLLOWUPS_DELIMITER not in terminal["content"]
        assert FOLLOWUPS[0] not in terminal["content"]


def test_condense_rewrites_a_follow_up_against_history(monkeypatch):
    """A follow-up must be resolved before routing, retrieval or grading see it."""
    seen_queries = []

    class _RecordingRetriever(_FakeRetriever):
        def retrieve_batch(self, requests, snapshot):
            seen_queries.extend(r.query for r in requests)
            return super().retrieve_batch(requests, snapshot)

    # condense -> standalone question, then route / rewrite / grade.
    fast = GenericFakeChatModel(
        messages=iter(["鹤岗的房价现在怎么样？", "RAG", "鹤岗 房价", "1"])
    )
    monkeypatch.setattr(graph_mod, "_fast_llm", fast)
    monkeypatch.setattr(
        graph_mod, "_generation_llm", GenericFakeChatModel(messages=iter([ANSWER]))
    )
    monkeypatch.setattr(graph_mod, "retriever", _RecordingRetriever())
    monkeypatch.setattr(
        graph_mod, "fetch_chunk_texts", lambda _schema, ids: dict.fromkeys(ids, "正文")
    )

    history = [
        {
            "question": "鹤岗为什么成了收缩型城市的代表？",
            "answer": "鹤岗是资源枯竭型城市。",
            "grounded": True,
        }
    ]
    events = _collect("那它的房价现在怎么样？", history)

    condensed = [e for e in events if e.get("step") == "condense"]
    assert condensed, "condense produced no step — the rewrite was not surfaced"
    assert "鹤岗" in condensed[0]["content"], (
        "the pronoun was not resolved against history; retrieval would search "
        "for the wrong subject"
    )
    assert seen_queries, "retrieval never ran"


def test_single_turn_skips_condense(stub_pipeline):
    """With no history there is nothing to resolve, so no extra model call."""
    events = _collect("鹤岗为什么成了收缩型城市的代表？")
    assert not [e for e in events if e.get("step") == "condense"]


@pytest.mark.parametrize(
    "raw,expected_answer,expected_followups",
    [
        (f"答案。\n{graph_mod.FOLLOWUPS_DELIMITER}\n问题一？\n问题二？", "答案。", 2),
        ("没有分隔符的普通答案。", "没有分隔符的普通答案。", 0),
        (f"答案。\n{graph_mod.FOLLOWUPS_DELIMITER}\n", "答案。", 0),
    ],
)
def test_followup_split_degrades_safely(raw, expected_answer, expected_followups):
    answer, followups = graph_mod._split_followups(raw)
    assert answer == expected_answer
    assert len(followups) == expected_followups


def test_uncited_answer_gets_a_source_list(monkeypatch):
    """A grounded answer must never reach the reader with nothing to check.

    The repair pass can only rewrite citations that are present. When the model
    omits them entirely — observed in the wild on an answer built from six
    documents — the result is fluent, specific and completely unattributed.
    """
    uncited = "SHEIN的成功来自珠三角产业链与数据驱动的供应链。"
    fast = GenericFakeChatModel(messages=iter(["RAG", "SHEIN 出海", "1,2,3"]))
    monkeypatch.setattr(graph_mod, "_fast_llm", fast)
    monkeypatch.setattr(
        graph_mod, "_generation_llm", GenericFakeChatModel(messages=iter([uncited]))
    )
    monkeypatch.setattr(graph_mod, "retriever", _FakeRetriever())
    monkeypatch.setattr(
        graph_mod, "fetch_chunk_texts", lambda _schema, ids: dict.fromkeys(ids, "正文")
    )

    events = _collect("SHEIN出海为什么成功？")
    terminal = next(e for e in events if e["type"] in ("answer_final", "answer_meta"))

    # Appending sources is invisible to the token stream, so the canonical text
    # has to be re-sent or the reader keeps the uncited version.
    assert terminal["type"] == "answer_final", (
        "sources were appended but answer_final was not sent, so the client "
        "would still be showing the uncited answer"
    )
    assert f"]({graph_mod.TRANSCRIPTS_BASE_URL}" in terminal["content"]
    assert "参考来源" in terminal["content"]
    assert uncited in terminal["content"], "the model's answer must be preserved"


def test_source_list_is_not_appended_when_the_model_cited(stub_pipeline):
    """The fallback must stay out of the way when the model behaved."""
    events = _collect("鹤岗为什么成了收缩型城市的代表？")
    answer = "".join(e["content"] for e in events if e["type"] == "answer_chunk")
    terminal = next(e for e in events if e["type"] in ("answer_final", "answer_meta"))
    text = terminal.get("content", answer)
    assert "参考来源" not in text


def test_citation_repair_covers_every_spelling():
    from langchain_core.documents import Document

    uri = "ChanJingPoBiJi/0001-0100/0067.md"
    url = "/transcripts/ChanJingPoBiJi/0001-0100/0067.md"
    section_url = f"{url}#{quote('芯片', safe='')}"
    section = f"[[产经破壁机67]]({section_url})"
    citation_map = graph_mod.build_citation_map(
        [
            Document(
                page_content="",
                metadata={
                    "doc_id": uri,
                    "title": "产经破壁机67",
                    "heading_anchor": "芯片",
                },
            )
        ]
    )
    raws = (
        # The source id without its section, the episode title, and 书名号.
        # This retrieval has a section, so none of these open the article start.
        f"见 [[{uri}]] 的分析",
        f"见 [[{uri}]](...) 的分析",
        # The model drops the .md suffix it was shown.
        f"见 [[{uri[:-3]}]] 的分析",
        f"见 [[{uri[:-3]}#芯片]] 的分析",
        f"见 [[{uri[:-3]}#不存在]] 的分析",
        "见 [[产经破壁机67]] 的分析",
        "见 《产经破壁机67》 的分析",
    )
    for raw in raws:
        repaired, count = graph_mod._repair_citations(raw, citation_map)
        assert count == 1, raw
        assert repaired == f"见（{section}） 的分析", raw

    for raw in (
        f"见 [[{uri}#芯片]] 的分析",
        "见 [[产经破壁机67#芯片]] 的分析",
    ):
        repaired, count = graph_mod._repair_citations(raw, citation_map)
        assert count == 1, raw
        assert repaired == f"见（{section}） 的分析", raw

    # An anchor this retrieval did not return still opens a retrieved section.
    invented, count = graph_mod._repair_citations(
        f"见 [[{uri}#不存在]] 的分析", citation_map
    )
    assert count == 1
    assert invented == f"见（{section}） 的分析"

    # A real book title shares the 《》 spelling but is not a known episode.
    untouched, count = graph_mod._repair_citations("他读了《三体》", citation_map)
    assert count == 0
    assert untouched == "他读了《三体》"


def test_citation_map_is_keyed_by_uri_and_title():
    from langchain_core.documents import Document

    doc_id = "CanKaoXinXi/0401-0500/0490.md"
    hegang = f"[[参考信息490]]({CITED_URL}#{quote('鹤岗', safe='')})"
    price = f"[[参考信息490]]({CITED_URL}#{quote('房价', safe='')})"
    first = Document(
        page_content="",
        metadata={
            "doc_id": doc_id,
            "title": "参考信息490",
            "heading_anchor": "鹤岗",
            "similarity": 0.4,
        },
    )
    second = Document(
        page_content="",
        metadata={
            "doc_id": doc_id,
            "title": "参考信息490",
            "heading_anchor": "房价",
            "similarity": 0.9,
        },
    )
    citations = graph_mod.build_citation_map([first, second])
    # The bare episode name opens the strongest section, not the article start.
    assert citations[doc_id] == price
    assert citations["参考信息490"] == price
    assert citations[f"{doc_id}#鹤岗"] == hegang
    assert citations[f"{doc_id}#房价"] == price
    assert citations["参考信息490#鹤岗"] == hegang
    assert citations["参考信息490#房价"] == price
    assert "#" in citations["参考信息490"].split("](", 1)[-1]


def test_backticks_around_a_citation_are_removed():
    from langchain_core.documents import Document

    uri = "CanKaoXinXi/0001-0100/0017.md"
    anchor = "俄军更换指挥官-美欧援助重装备"
    documents = [
        Document(
            page_content="",
            metadata={
                "doc_id": uri,
                "title": "参考信息17",
                "heading_anchor": anchor,
            },
        )
    ]
    citation_map = graph_mod.build_citation_map(documents)
    raw = f"据 `[[{uri}#{anchor}]]` 提供的信息"
    repaired, count = graph_mod._repair_citations(raw, citation_map, documents)
    assert count == 1
    assert "`" not in repaired
    assert repaired.startswith("据（[[参考信息17]](/transcripts/")
    assert repaired.endswith(")） 提供的信息")
    assert quote(anchor, safe="") in repaired


def test_heading_text_is_not_a_citation_key():
    from langchain_core.documents import Document

    doc_id = "ShuiQianXiaoXi/0501-0600/0588.md"
    documents = [
        Document(
            page_content="",
            metadata={
                "doc_id": doc_id,
                "title": "睡前消息588",
                "heading_anchor": "地方债与政信债",
            },
        ),
        Document(
            page_content="",
            metadata={
                "doc_id": "ShuiQianXiaoXi/0001-0100/0013.5.md",
                "title": "睡前消息13.5",
                "heading_anchor": "",
            },
        ),
    ]
    citations = graph_mod.build_citation_map(documents)
    assert "地方债与政信债" not in citations
    assert f"{doc_id}#地方债与政信债" in citations
    assert graph_mod._source_id(doc_id, "地方债与政信债") == f"{doc_id}#地方债与政信债"
    assert graph_mod._source_id("ShuiQianXiaoXi/0001-0100/0013.5.md", "") == (
        "ShuiQianXiaoXi/0001-0100/0013.5.md"
    )


def test_bare_title_opens_the_heading_the_paragraph_is_about():
    from langchain_core.documents import Document

    doc_id = "ShuiQianXiaoXi/0501-0600/0588.md"
    url = "/transcripts/ShuiQianXiaoXi/0501-0600/0588.md"
    documents = [
        Document(
            page_content="",
            metadata={
                "doc_id": doc_id,
                "title": "睡前消息588",
                "heading": "地方债与政信债",
                "heading_anchor": "地方债与政信债",
                "similarity": 0.4,
            },
        ),
        Document(
            page_content="",
            metadata={
                "doc_id": doc_id,
                "title": "睡前消息588",
                "heading": "病毒绕着走的城市",
                "heading_anchor": "病毒绕着走的城市",
                "similarity": 0.9,
            },
        ),
    ]
    citation_map = graph_mod.build_citation_map(documents)
    # The chip sits inside the sentence, in parentheses, before the period.
    debt = f"地方债和政信债不是一回事（[[睡前消息588 · 地方债与政信债]]({url})）。"
    virus = f"柳州被称为病毒绕着走的城市（[[睡前消息588 · 病毒绕着走的城市]]({url})）。"

    repaired, count = graph_mod._repair_citations(
        "地方债和政信债不是一回事。[[睡前消息588]]", citation_map, documents
    )
    assert count == 1
    assert repaired == debt.replace(url, url + "#" + quote("地方债与政信债", safe=""))

    repaired, _count = graph_mod._repair_citations(
        "柳州被称为病毒绕着走的城市。[[睡前消息588]]", citation_map, documents
    )
    assert repaired == virus.replace(
        url, url + "#" + quote("病毒绕着走的城市", safe="")
    )

    # No heading wins. The link still opens a retrieved section, the strongest one.
    repaired, _count = graph_mod._repair_citations(
        "节目讨论了债务。[[睡前消息588]]", citation_map, documents
    )
    assert repaired == (
        "节目讨论了债务（[[睡前消息588 · 病毒绕着走的城市]]("
        + url
        + "#"
        + quote("病毒绕着走的城市", safe="")
        + ")）。"
    )


def test_title_falls_back_to_the_general_rule_when_the_db_row_is_missing():
    from langchain_core.documents import Document

    # A chunk indexed before the title sync ran: LEFT JOIN yields title=None.
    doc = Document(
        page_content="",
        metadata={"doc_id": "CanKaoXinXi/0401-0500/0490.md", "title": None},
    )
    assert graph_mod.build_citation_map([doc])["CanKaoXinXi/0401-0500/0490.md"] == (
        f"[[参考信息490]]({CITED_URL})"
    )


def test_uri_citations_are_rewritten_into_titled_links(stub_pipeline):
    """The raw stream carries URIs; what the reader ends up with carries titles.

    Because the model now writes URIs, every cited answer is necessarily
    repaired, so the server must always re-send it — the streamed text is never
    the text to display. The client applies the same substitution per render
    tick from the citations event, and this pins the server half of that.
    """
    events = _collect("鹤岗为什么成了收缩型城市的代表？")

    streamed = "".join(e["content"] for e in events if e["type"] == "answer_chunk")
    assert f"[[{CITED_URI}]]" in streamed, "the model should have written the URI"

    terminal = next(e for e in events if e["type"] in ("answer_final", "answer_meta"))
    assert terminal["type"] == "answer_final", (
        "a URI citation was repaired, so the canonical answer has to be re-sent "
        "or the reader keeps seeing raw URIs"
    )
    assert (
        f"[[参考信息490 · 大家好，欢迎收看睡前消息]]({CITED_URL}#{quote('鹤岗', safe='')})"
        in terminal["content"]
    )
    assert f"[[{CITED_URI}]]" not in terminal["content"]
    urls = next(e for e in events if e["type"] == "citations")["urls"]
    assert f"{CITED_URI}#鹤岗" in urls


def test_citations_event_carries_titles_and_urls(stub_pipeline):
    """The client needs both values to linkify while streaming."""
    events = _collect("鹤岗为什么成了收缩型城市的代表？")
    urls = next(e for e in events if e["type"] == "citations")["urls"]

    section = f"{CITED_URI}#鹤岗"
    section_url = f"{CITED_URL}#{quote('鹤岗', safe='')}"
    # The bare episode name opens that section. The title stays the episode
    # title; the chip label adds the section name.
    assert urls[CITED_URI] == {
        "title": "参考信息490",
        "label": "参考信息490 · 大家好，欢迎收看睡前消息",
        "url": section_url,
    }
    assert urls["参考信息490"] == urls[CITED_URI]
    assert urls[section]["title"] == "参考信息490"
    assert urls[section]["url"] == section_url
    assert "鹤岗" not in urls


def test_adjacent_duplicate_citations_collapse_to_one():
    a = "[[高见54]](/transcripts/G/0054.md#%E7%AB%A0A)"
    b = "[[高见54]](/transcripts/G/0054.md#%E7%AB%A0B)"
    c = "[[参考信息1]](/transcripts/C/0001.md)"
    dedupe = graph_mod._dedupe_citation_runs
    # Repeats inside one run collapse, order of first appearance is kept.
    assert dedupe(f"句子。{a} {b} {a}") == f"句子。{a} {b}"
    assert dedupe(f"句子。{a}{a}") == f"句子。{a}"
    # The same link in two different runs supports two sentences and stays.
    assert dedupe(f"甲。{a} 乙。{a}") == f"甲。{a} 乙。{a}"
    # A different link is never merged, and a lone link is untouched.
    assert dedupe(f"{a} {c}") == f"{a} {c}"
    assert dedupe(a) == a
    assert dedupe("没有引用") == "没有引用"


def test_section_label_is_plain_and_at_most_twelve_characters():
    label = graph_mod._section_label
    assert label("总结") == "总结"
    # Exactly at the limit stays whole.
    assert label("一二三四五六七八九十甲乙") == "一二三四五六七八九十甲乙"
    # Over the limit: 11 characters then a single ellipsis character, 12 in all.
    cut = label("一二三四五六七八九十甲乙丙")
    assert cut == "一二三四五六七八九十甲…"
    assert len(cut) == 12
    # Markdown source becomes plain text, and ASCII brackets are dropped.
    assert label("**加粗** [链接](http://x) [^1]") == "加粗 链接"
    assert label("[方括号]内容") == "方括号内容"
    assert label("多个   空白\t折叠") == "多个 空白 折叠"
    assert label(None) == ""
    assert label("") == ""


def test_chip_label_adds_the_section_only_when_the_link_has_one():
    citation_label = graph_mod._citation_label
    assert citation_label("高见54", "总结", "总结") == "高见54 · 总结"
    # A whole-document link (no anchor) stays the bare title.
    assert citation_label("高见54", "总结", "") == "高见54"
    # An anchor whose heading is unknown falls back to the bare title.
    assert citation_label("高见54", None, "总结") == "高见54"


def test_citation_runs_sit_inside_the_sentence_in_parentheses():
    a = "[[参考信息29 · 招生]](/transcripts/C/0029.md#a)"
    b = "[[参考信息211 · 在园]](/transcripts/C/0211.md#b)"
    place = graph_mod._place_citation_runs

    # After the period: moved before it, wrapped in full-width parentheses.
    assert place(f"竞争很激烈。{a} 一个反差。{b}") == (
        f"竞争很激烈（{a}）。 一个反差（{b}）。"
    )
    # Several chips form one group, and the other sentence-final marks work too.
    assert place(f"竞争很激烈。 {a} {b} 下一句") == f"竞争很激烈（{a} {b}）。 下一句"
    assert place(f"真的吗？{a} 是的！{b}") == f"真的吗（{a}）？ 是的（{b}）！"
    assert place(f"成立；{a}") == f"成立（{a}）；"
    # Already before the period: only wrapped.
    assert place(f"竞争很激烈{a}。下一句") == f"竞争很激烈（{a}）。下一句"
    # Mid-sentence and without punctuation: wrapped where it is.
    assert place(f"句子，{a}继续") == f"句子，（{a}）继续"
    assert place(f"他说“好。”{a}") == f"他说“好。”（{a}）"
    # Each paragraph is handled on its own and keeps its break.
    assert place(f"第一段。{a}\n\n第二段。{b}") == f"第一段（{a}）。\n\n第二段（{b}）。"


def test_citation_placement_leaves_alone_what_it_must_and_is_idempotent():
    a = "[[参考信息29 · 招生]](/transcripts/C/0029.md#a)"
    b = "[[参考信息211 · 在园]](/transcripts/C/0211.md#b)"
    place = graph_mod._place_citation_runs

    # A source list (every line is a marker plus a link) is not wrapped.
    listing = f"**参考来源**\n\n- {a}\n- {b}\n"
    assert place(listing) == listing
    assert place(f"1. {a}\n2. 内容。{b}") == f"1. {a}\n2. 内容（{b}）。"
    # Inside an existing parenthetical: untouched.
    assert place(f"见（来源：{a}）。下一句") == f"见（来源：{a}）。下一句"
    assert place("没有任何引用。") == "没有任何引用。"

    for text in (
        f"竞争很激烈。{a} 下一句。{b}",
        f"竞争很激烈。 {a} {b} 下一句",
        f"句子，{a}继续",
        listing,
    ):
        once = place(text)
        assert place(once) == once


def test_repaired_answer_places_the_chip_before_the_period():
    from langchain_core.documents import Document

    uri = "CanKaoXinXi/0001-0100/0029.md"
    documents = [
        Document(
            page_content="",
            metadata={
                "doc_id": uri,
                "title": "参考信息29",
                "heading": "首轮幼儿园关停潮到来",
                "heading_anchor": "首轮幼儿园关停潮到来",
            },
        )
    ]
    citation_map = graph_mod.build_citation_map(documents)
    repaired, _count = graph_mod._repair_citations(
        f"生源竞争也很激烈。[[{uri}]] 一个反差是新生儿减少。", citation_map, documents
    )
    label = "[[参考信息29 · 首轮幼儿园关停潮到来]]"
    assert repaired.startswith("生源竞争也很激烈（" + label)
    assert "）。 一个反差是新生儿减少。" in repaired


def test_a_repeated_citation_is_kept_once_for_a_run_of_sentences():
    p = "[[参考信息598 · 物业费]](/transcripts/C/0598.md#p)"
    q = "[[睡前消息1054 · 缴费]](/transcripts/S/1054.md#q)"
    r = "[[睡前消息1054 · 别处]](/transcripts/S/1054.md#r)"
    collapse = graph_mod._collapse_repeated_citations
    place = graph_mod._place_citation_runs

    # Consecutive sentences and semicolon clauses from one source: cited once, at the last.
    text = f"甲。{p} 乙，成立；{q} 丙也成立。{q} 丁认为。{q}"
    assert collapse(text) == f"甲。{p} 乙，成立；丙也成立。丁认为。{q}"
    assert place(collapse(text)) == f"甲（{p}）。 乙，成立；丙也成立。丁认为（{q}）。"
    # A different source in between keeps both mentions: they support different sentences.
    assert collapse(f"甲。{q} 乙。{p} 丙。{q}") == f"甲。{q} 乙。{p} 丙。{q}"
    # Another section of the same episode is another link, not a repeat.
    assert collapse(f"甲。{q} 乙。{r}") == f"甲。{q} 乙。{r}"
    # A new paragraph starts over.
    assert collapse(f"甲。{q}\n\n乙。{q}") == f"甲。{q}\n\n乙。{q}"
    # A run of several chips repeats as a whole, not chip by chip.
    assert collapse(f"甲。{q} {p} 乙。{q} {p} 丙。") == f"甲。乙。{q} {p} 丙。"
    assert collapse(f"甲。{q} {p} 乙。{q}") == f"甲。{q} {p} 乙。{q}"
    # Source lists and runs inside a parenthetical are not touched.
    listing = f"**参考来源**\n\n- {q}\n- {q}\n"
    assert collapse(listing) == listing
    inside = f"见（来源：{q}）。又见（来源：{q}）。"
    assert collapse(inside) == inside
    assert collapse("没有引用。") == "没有引用。"
    # Applying the whole pipeline twice changes nothing.
    once = place(collapse(text))
    assert place(collapse(once)) == once


def test_repaired_answer_cites_a_series_of_sentences_once():
    from langchain_core.documents import Document

    uri = "ShuiQianXiaoXi/1001-1100/1054.md"
    documents = [
        Document(
            page_content="",
            metadata={
                "doc_id": uri,
                "title": "睡前消息1054",
                "heading": "缴费",
                "heading_anchor": "缴费",
            },
        )
    ]
    citation_map = graph_mod.build_citation_map(documents)
    cite = f"[[{uri}]]"
    repaired, _count = graph_mod._repair_citations(
        f"甲认为。{cite} 乙也认为；{cite} 丙还指出。{cite}", citation_map, documents
    )
    assert repaired.count("[[睡前消息1054") == 1
    assert repaired.startswith("甲认为。乙也认为；丙还指出（[[睡前消息1054 · 缴费]]")
    assert repaired.endswith("）。")
