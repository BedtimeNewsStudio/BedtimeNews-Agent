"""Snapshot selection (design 7.1-7.4) and the per-snapshot retrieval cache.

Selection tests need PostgreSQL: set PGTEST_HOST (and optionally PGTEST_PORT,
PGTEST_USER, PGTEST_PASSWORD); without it they are skipped. The cache test
runs everywhere.
"""

import os
import uuid

import psycopg2
import psycopg2.extras
import pytest
from psycopg2.extras import Json
from src import retriever as retriever_mod
from src import snapshots, vector_db
from src.models import RetrieveRequest
from src.snapshots import Snapshot, SnapshotManager, select_snapshot

PGTEST_HOST = os.environ.get("PGTEST_HOST")
needs_db = pytest.mark.skipif(not PGTEST_HOST, reason="set PGTEST_HOST")
SPACE = "test-embedding-model@2560"


def _kw(dbname):
    return {
        "host": PGTEST_HOST,
        "port": int(os.environ.get("PGTEST_PORT", "5432")),
        "user": os.environ.get("PGTEST_USER", "postgres"),
        "password": os.environ.get("PGTEST_PASSWORD", ""),
        "dbname": dbname,
    }


@pytest.fixture
def db(monkeypatch):
    name = f"bn_agent_{uuid.uuid4().hex[:10]}_local"
    admin = psycopg2.connect(**_kw("postgres"))
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    conn = psycopg2.connect(**_kw(name))
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
    kw = _kw(name)
    s = snapshots.settings
    monkeypatch.setattr(s, "postgres_host", kw["host"])
    monkeypatch.setattr(s, "postgres_port", kw["port"])
    monkeypatch.setattr(s, "postgres_user", kw["user"])
    monkeypatch.setattr(s, "postgres_password", kw["password"])
    monkeypatch.setattr(s, "postgres_db", name)
    monkeypatch.setattr(s, "postgres_agent_password", "")
    monkeypatch.setattr(s, "embedding_dim", 2560)
    monkeypatch.setattr(s, "rag_snapshot", "")
    monkeypatch.setattr(s.embedding, "space_id", "")
    vector_db.close_connection_pool()
    yield conn
    vector_db.close_connection_pool()
    conn.close()
    with admin.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    admin.close()


REGISTRY = """
CREATE SCHEMA rag_meta;
CREATE TABLE rag_meta.snapshots (
    snapshot_id TEXT PRIMARY KEY, schema_name TEXT NOT NULL UNIQUE,
    format_version INTEGER NOT NULL, pipeline_fingerprint TEXT NOT NULL,
    embedding_space TEXT NOT NULL, embedding_model TEXT NOT NULL,
    embedding_dim INTEGER NOT NULL, normalization_version INTEGER,
    chunker_version INTEGER, source_commit TEXT, base_snapshot_id TEXT,
    builder_version TEXT NOT NULL, status TEXT NOT NULL,
    pinned BOOLEAN NOT NULL DEFAULT false, published_at TIMESTAMPTZ NOT NULL,
    retired_at TIMESTAMPTZ, index_params JSONB, stats JSONB);
CREATE TABLE rag_meta.indexer_status (
    id BOOLEAN PRIMARY KEY DEFAULT true, last_run_at TIMESTAMPTZ,
    last_result TEXT, last_error TEXT, last_published_at TIMESTAMPTZ,
    consecutive_failures INTEGER NOT NULL DEFAULT 0);
INSERT INTO rag_meta.indexer_status (id, last_result) VALUES (true, 'published');
"""


def register(
    conn,
    sid,
    *,
    fmt=1,
    space=SPACE,
    minutes_ago=0,
    status="published",
    search_version=None,
):
    stats = None if search_version is None else Json({"search_version": search_version})
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO rag_meta.snapshots (snapshot_id, schema_name, "
            "format_version, pipeline_fingerprint, embedding_space, "
            "embedding_model, embedding_dim, builder_version, status, "
            "published_at, stats) VALUES (%s, %s, %s, 'fp', %s, 'm', 2560, 'test', %s, "
            "now() - make_interval(mins => %s), %s)",
            (sid, f"rag_{sid}", fmt, space, status, minutes_ago, stats),
        )


def select(conn):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        return select_snapshot(cur)


def selected(conn):
    """The snapshot `select` picked; fails the test when none was selected."""
    snap = select(conn)[0]
    assert snap is not None
    return snap


@needs_db
def test_newest_published_in_my_space_wins(db):
    db.cursor().execute(REGISTRY)
    register(db, "s_old", minutes_ago=60)
    register(db, "s_new", minutes_ago=1)
    register(db, "s_other_space", space="other@1024")
    register(db, "s_retired", status="retired")
    snap, reason, status = select(db)
    assert snap is not None
    assert status is not None
    assert (snap.snapshot_id, reason) == ("s_new", None)
    assert status["last_result"] == "published"


@needs_db
def test_higher_supported_format_beats_newer(db, monkeypatch):
    db.cursor().execute(REGISTRY)
    register(db, "s_v2", fmt=2, minutes_ago=30)
    register(db, "s_v1", fmt=1, minutes_ago=1)
    register(db, "s_v3", fmt=3)
    register(db, "s_v4", fmt=4)
    register(db, "s_v5", fmt=5)
    register(db, "s_v6", fmt=6)
    # Format 5 serves postings search; a future format remains unreadable.
    assert selected(db).snapshot_id == "s_v5"
    # Older processes continue selecting their own highest supported format.
    monkeypatch.setattr(snapshots, "SUPPORTED_FORMATS", frozenset({1, 2}))
    assert selected(db).snapshot_id == "s_v2"
    # A process that only supports {1} keeps reading format 1.
    monkeypatch.setattr(snapshots, "SUPPORTED_FORMATS", frozenset({1}))
    assert selected(db).snapshot_id == "s_v1"


@needs_db
def test_no_snapshot_in_my_space_is_not_ready(db):
    db.cursor().execute(REGISTRY)
    register(db, "s_other", space="other@1024")
    snap, reason, _ = select(db)
    assert snap is None
    assert reason
    assert SPACE in reason


@needs_db
def test_pinned_snapshot(db, monkeypatch):
    db.cursor().execute(REGISTRY)
    register(db, "s_a", minutes_ago=10)
    register(db, "s_b")
    monkeypatch.setattr(snapshots.settings, "rag_snapshot", "s_a")
    assert selected(db).snapshot_id == "s_a"
    db.cursor().execute(
        "UPDATE rag_meta.snapshots SET status = 'retired' WHERE snapshot_id = 's_a'"
    )
    snap, reason, _ = select(db)
    assert snap is None
    assert reason
    assert "retired" in reason
    monkeypatch.setattr(snapshots.settings, "rag_snapshot", "s_missing")
    reason = select(db)[1]
    assert reason
    assert "does not exist" in reason


@needs_db
def test_implicit_legacy_before_adoption(db, monkeypatch):
    cur = db.cursor()
    cur.execute(
        "CREATE SCHEMA rag; CREATE TABLE rag.document_chunks "
        "(chunk_id text, embedding halfvec(2560))"
    )
    snap, reason, _ = select(db)
    assert snap is None
    assert reason
    assert "empty" in reason
    cur.execute(
        "INSERT INTO rag.document_chunks VALUES "
        "('c', array_fill(0.1::real, ARRAY[2560])::halfvec)"
    )
    snap, reason, _ = select(db)
    assert snap is not None
    assert (snap.snapshot_id, snap.schema_name, reason) == ("legacy", "rag", None)
    monkeypatch.setattr(snapshots.settings, "embedding_dim", 1024)
    snap, reason, _ = select(db)
    assert snap is None
    assert reason
    assert "2560" in reason


@needs_db
def test_empty_database_is_not_ready(db):
    snap, reason, _ = select(db)
    assert snap is None
    assert reason


@needs_db
def test_manager_switches_and_falls_back_on_retire(db):
    db.cursor().execute(REGISTRY)
    register(db, "s1", minutes_ago=5)
    manager = SnapshotManager()
    manager.refresh()
    pinned_for_request = manager.require()
    assert pinned_for_request.snapshot_id == "s1"

    register(db, "s2")
    manager.refresh()
    assert manager.require().snapshot_id == "s2"
    # A request that started before the switch still holds its snapshot.
    assert pinned_for_request.snapshot_id == "s1"

    db.cursor().execute(
        "UPDATE rag_meta.snapshots SET status = 'retired' WHERE snapshot_id = 's2'"
    )
    manager.refresh()
    assert manager.require().snapshot_id == "s1"
    ready, body = manager.health()
    assert ready
    assert body["snapshot"]["id"] == "s1"
    assert body["indexer_status"]["last_result"] == "published"


def test_unreachable_database_keeps_agent_up_but_not_ready(monkeypatch):
    manager = SnapshotManager()

    def broken():
        raise psycopg2.OperationalError("connection refused")

    monkeypatch.setattr(vector_db, "read_cursor", broken)
    manager.refresh()
    ready, body = manager.health()
    assert not ready
    assert "database unreachable" in body["reason"]


def test_retrieval_cache_is_per_snapshot(monkeypatch):
    calls = []

    def search(schema, **_kwargs):
        calls.append(schema)
        return [
            {
                "chunk_id": f"{schema}-chunk",
                "doc_id": "doc.md",
                "title": "t",
                "chunk_index": 0,
                "heading": None,
                "heading_anchor": "",
                "text": "x",
                "word_count": 1,
                "similarity": 0.9,
            }
        ]

    monkeypatch.setattr(retriever_mod, "search_similar_chunks", search)
    r = retriever_mod._Retriever.__new__(retriever_mod._Retriever)
    r._result_cache = retriever_mod.LRUCache(capacity=10)
    monkeypatch.setattr(r, "_generate_embedding", lambda _text: [0.0])
    monkeypatch.setattr(
        r, "_generate_embeddings_batch", lambda texts: [[0.0] for _ in texts]
    )
    request = RetrieveRequest(query="q", match_count=5, match_threshold=0.1)
    old = Snapshot("s1", "rag_s1", 1, SPACE)
    new = Snapshot("s2", "rag_s2", 1, SPACE)

    assert r.retrieve(request, old).results[0].chunk_id == "rag_s1-chunk"
    assert r.retrieve(request, old).results[0].chunk_id == "rag_s1-chunk"
    assert r.retrieve_batch([request], new)[0].results[0].chunk_id == "rag_s2-chunk"
    assert calls == ["rag_s1", "rag_s2"], "second s1 call is cached; s2 is not"


def _sql_text(query) -> str:
    seq = getattr(query, "seq", None)
    if seq is not None:
        return "".join(_sql_text(part) for part in seq)
    string = getattr(query, "string", None)
    if isinstance(string, str):
        return string
    strings = getattr(query, "strings", None)
    if strings:
        return ".".join(strings)
    return ""


def test_format_1_sql_omits_heading_anchor_and_format_2_selects_it(monkeypatch):
    captured = []

    class _Capture:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return self

        def execute(self, query, _params):
            captured.append(_sql_text(query))

        def fetchall(self):
            return []

    monkeypatch.setattr(vector_db, "_Connection", _Capture)
    vector_db.search_similar_chunks("rag_s1", [0.1], 0.2, 3, format_version=1)
    vector_db.search_similar_chunks("rag_s2", [0.1], 0.2, 3, format_version=2)
    assert "heading_anchor" not in captured[0]
    assert "heading_anchor" in captured[1]
