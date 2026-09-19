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


def test_projection_sync_uses_its_own_source_hash_state(tmp_path, monkeypatch):
    uri = "channel/0001-0100/0001.md"
    path = tmp_path / uri
    path.parent.mkdir(parents=True)
    raw = b"# T\n\n## \xe6\xad\xa3\xe6\x96\x87\n\nBody\n"
    path.write_bytes(raw)
    source_hash = __import__("hashlib").sha256(raw).hexdigest()
    monkeypatch.setattr(pipeline, "CONTENTS_DIR", tmp_path)
    monkeypatch.setattr(pipeline, "get_transcript_states", lambda: {})
    published = []
    monkeypatch.setattr(pipeline, "upsert_transcript_projection", published.append)
    removed = []
    monkeypatch.setattr(pipeline, "delete_transcript_projection", removed.append)
    title_syncs = []
    monkeypatch.setattr(pipeline, "sync_transcript_titles", title_syncs.append)

    counts = pipeline.sync_transcript_projections(
        {uri}, {uri: source_hash}, {uri: "标准标题"}
    )

    assert counts == (1, 0)
    assert published[0].doc_id == uri
    assert published[0].source_hash == source_hash
    assert published[0].projection_version == 3
    assert removed == []
    assert title_syncs == [{uri: "标准标题"}]


def test_projection_sync_rebuilds_old_renderer_version(tmp_path, monkeypatch):
    uri = "channel/0001-0100/0001.md"
    path = tmp_path / uri
    path.parent.mkdir(parents=True)
    raw = b"# T\n\n## \xe6\xad\xa3\xe6\x96\x87\n\nBody\n"
    path.write_bytes(raw)
    source_hash = __import__("hashlib").sha256(raw).hexdigest()
    monkeypatch.setattr(pipeline, "CONTENTS_DIR", tmp_path)
    monkeypatch.setattr(
        pipeline, "get_transcript_states", lambda: {uri: (source_hash, 0)}
    )
    published = []
    monkeypatch.setattr(pipeline, "upsert_transcript_projection", published.append)
    monkeypatch.setattr(pipeline, "delete_transcript_projection", lambda _uri: None)
    monkeypatch.setattr(pipeline, "sync_transcript_titles", lambda _titles: None)

    counts = pipeline.sync_transcript_projections(
        {uri}, {uri: source_hash}, {uri: "Title"}
    )

    assert counts == (1, 0)
    assert published[0].projection_version == 3


def test_projection_sync_removes_deleted_rows(monkeypatch):
    monkeypatch.setattr(
        pipeline,
        "get_transcript_states",
        lambda: {"deleted.md": ("old", 3), "kept.md": ("same", 3)},
    )
    removed = []
    monkeypatch.setattr(pipeline, "delete_transcript_projection", removed.append)
    monkeypatch.setattr(
        pipeline,
        "upsert_transcript_projection",
        lambda _projection: pytest.fail("unchanged projection was rebuilt"),
    )
    monkeypatch.setattr(pipeline, "sync_transcript_titles", lambda _titles: None)

    counts = pipeline.sync_transcript_projections(
        {"kept.md"}, {"kept.md": "same"}, {"kept.md": "Kept"}
    )

    assert counts == (0, 1)
    assert removed == ["deleted.md"]


def test_reader_sync_still_runs_when_rag_sync_fails(monkeypatch):
    changes = ChangeSet(
        added={"doc.md"}, current_source_hashes={"doc.md": "source-hash"}
    )
    monkeypatch.setattr(pipeline, "_validate_scope_safety", lambda: None)
    monkeypatch.setattr(pipeline, "sync_repository", lambda: None)
    monkeypatch.setattr(pipeline, "scan_files", lambda: {"doc.md"})
    monkeypatch.setattr(pipeline, "detect_changes", lambda _files: changes)
    monkeypatch.setattr(
        pipeline, "sync_document_titles", lambda _files: {"doc.md": "Title"}
    )
    monkeypatch.setattr(
        pipeline,
        "process_deletions",
        lambda _deleted: (_ for _ in ()).throw(RuntimeError("RAG unavailable")),
    )
    projection_calls = []
    monkeypatch.setattr(
        pipeline,
        "sync_transcript_projections",
        lambda *args: projection_calls.append(args) or (1, 0),
    )
    monkeypatch.setattr(pipeline, "close_connection_pool", lambda: None)

    with pytest.raises(RuntimeError, match="RAG synchronization failed"):
        pipeline.main()

    assert projection_calls == [
        ({"doc.md"}, {"doc.md": "source-hash"}, {"doc.md": "Title"})
    ]
