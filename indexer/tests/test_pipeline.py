"""Pipeline behavior around source-only changes and failure safety."""

import pytest
from src import pipeline
from src.change_detector import ChangeSet
from src.document_loader import BODY_NORMALIZATION_VERSION
from src.models import Chunk, Document, LoadedIndexableSource


def _source(uri="doc.md"):
    return LoadedIndexableSource(
        document=Document("/tmp/doc.md", uri, "doc", "正文"),
        source_hash="source",
        body_hash="body",
        body_normalization_version=BODY_NORMALIZATION_VERSION,
    )


def test_source_only_change_records_history_without_embedding(monkeypatch):
    source = _source()
    changes = ChangeSet(source_only={"doc.md"}, loaded_sources={"doc.md": source})
    calls = []
    monkeypatch.setattr(
        pipeline,
        "record_source_only_change",
        lambda **kwargs: calls.append(kwargs),
    )
    monkeypatch.setattr(
        pipeline,
        "generate_embeddings",
        lambda _texts: pytest.fail("source-only edit called embedding provider"),
    )
    monkeypatch.setattr(
        pipeline,
        "replace_document_index",
        lambda **_kwargs: pytest.fail("source-only edit replaced chunks"),
    )

    pipeline.process_source_only_changes(changes)

    assert calls == [
        {
            "file_path": "doc.md",
            "source_hash": "source",
            "body_hash": "body",
            "body_normalization_version": BODY_NORMALIZATION_VERSION,
        }
    ]


def test_embedding_failure_happens_before_database_replacement(monkeypatch):
    source = _source()
    changes = ChangeSet(added={"doc.md"}, loaded_sources={"doc.md": source})
    chunk = Chunk("doc_chunk_000", "doc.md", 0, "正文", 2)
    monkeypatch.setattr(pipeline, "chunk_document", lambda _document: [chunk])
    monkeypatch.setattr(
        pipeline,
        "generate_embeddings",
        lambda _texts: (_ for _ in ()).throw(RuntimeError("provider failed")),
    )
    monkeypatch.setattr(
        pipeline,
        "replace_document_index",
        lambda **_kwargs: pytest.fail(
            "database was changed before embeddings returned"
        ),
    )

    with pytest.raises(RuntimeError, match="provider failed"):
        pipeline.process_content_changes(changes)


def test_sample_scope_refuses_non_local_database(monkeypatch):
    monkeypatch.setattr(pipeline.settings, "indexer_scope", "sample")
    monkeypatch.setattr(pipeline.settings, "postgres_db", "postgres_db")
    with pytest.raises(RuntimeError, match="local-only"):
        pipeline._validate_scope_safety()
