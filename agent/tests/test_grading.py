"""Relevance grading maps the grader's document numbers back to documents."""

from types import SimpleNamespace

from langchain_core.documents import Document
from src import graph as graph_mod


def _docs(n: int) -> list[Document]:
    return [
        Document(page_content=f"text {i}", metadata={"chunk_id": f"c{i}"})
        for i in range(n)
    ]


def test_parallel_batches_number_documents_from_one(monkeypatch):
    # 40 documents over a threshold of 30 -> batches of 13, 13, 13, 1.
    monkeypatch.setattr(graph_mod.settings, "grading_parallel_threshold", 30)
    prompts: list[str] = []

    def fake_parallel(llm, messages_list, max_concurrent=3):
        prompts.extend(m[1].content for m in messages_list)
        return [SimpleNamespace(content="1") for _ in messages_list]

    monkeypatch.setattr(graph_mod, "_parallel_llm_calls", fake_parallel)

    state = {"question": "q", "standalone_question": "q", "documents": _docs(40)}
    result = graph_mod._documents_grade_node(state)

    assert all("Document 1 [" in p for p in prompts)
    assert [d.metadata["chunk_id"] for d in result["relevant_documents"]] == [
        "c0",
        "c13",
        "c26",
        "c39",
    ]
