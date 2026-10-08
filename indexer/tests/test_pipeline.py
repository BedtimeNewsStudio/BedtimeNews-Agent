"""Snapshot build/publish behaviour against a real PostgreSQL + pgvector.

These tests need a server: set PGTEST_HOST (and optionally PGTEST_PORT,
PGTEST_USER, PGTEST_PASSWORD) to a superuser login on a Postgres with the
pgvector extension available. Each test gets its own throwaway database. CI
runs them against a pgvector service container; without PGTEST_HOST they are
skipped. Embeddings come from a deterministic in-process fake.
"""

import hashlib
import math
import os
import random
import uuid
from datetime import UTC, datetime, timedelta

import psycopg2
import pytest
from src import (
    builder,
    catalog,
    change_detector,
    chunker,
    document_loader,
    embeddings,
    file_scanner,
    paths,
    pipeline,
    uri_mapping,
)
from src.chunker import chunk_document
from src.run_lock import run_lock
from src.settings import settings

PGTEST_HOST = os.environ.get("PGTEST_HOST")
pytestmark = pytest.mark.skipif(
    not PGTEST_HOST, reason="set PGTEST_HOST to run database tests"
)

DIM = 16
MODEL = "test-embedding-model"


def _pg_kwargs(dbname: str) -> dict:
    return {
        "host": PGTEST_HOST,
        "port": int(os.environ.get("PGTEST_PORT", "5432")),
        "user": os.environ.get("PGTEST_USER", "postgres"),
        "password": os.environ.get("PGTEST_PASSWORD", ""),
        "dbname": dbname,
    }


def fake_vector(text: str, salt: str = "") -> list[float]:
    rnd = random.Random(hashlib.sha256((salt + text).encode()).digest())
    v = [rnd.gauss(0, 1) for _ in range(DIM)]
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


class FakeEmbeddings:
    """Records every call; ``salt`` simulates a different model."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.salt = ""

    def __call__(self, texts):
        self.calls.append(list(texts))
        return [fake_vector(t, self.salt) for t in texts]

    @property
    def texts(self) -> int:
        return sum(len(c) for c in self.calls)


@pytest.fixture
def database(monkeypatch):
    name = f"bn_test_{uuid.uuid4().hex[:10]}_local"
    admin = psycopg2.connect(**_pg_kwargs("postgres"))
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    kw = _pg_kwargs(name)
    monkeypatch.setattr(settings, "postgres_host", kw["host"])
    monkeypatch.setattr(settings, "postgres_port", kw["port"])
    monkeypatch.setattr(settings, "postgres_user", kw["user"])
    monkeypatch.setattr(settings, "postgres_password", kw["password"])
    monkeypatch.setattr(settings, "postgres_db", name)
    monkeypatch.setattr(settings, "embedding_dim", DIM)
    monkeypatch.setattr(settings.embedding, "model", MODEL)
    monkeypatch.setattr(settings.embedding, "space_id", "")
    monkeypatch.setattr(settings, "pgdata_path", "")
    monkeypatch.setattr(settings, "postgres_agent_password", "")
    yield name
    with admin.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    admin.close()


@pytest.fixture
def query(database):
    def run(sql, params=None):
        conn = psycopg2.connect(**_pg_kwargs(database))
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall() if cur.description else None
            conn.commit()
            return rows
        finally:
            conn.close()

    return run


def _body(seed: str, paragraphs: int = 2) -> str:
    # Enough CJK characters per paragraph to clear the 50-word minimum.
    rnd = random.Random(seed)
    chars = "城投债务房价财政教育医疗交通能源产业人口就业"
    return "\n\n".join(
        "".join(rnd.choice(chars) for _ in range(120)) for _ in range(paragraphs)
    )


def transcript(seed: str, *, title: str | None = None, appendix: str = "订正") -> str:
    return (
        f"# {title or seed}\n\n**发布日期** 2026-01-01\n\n## 正文\n\n{_body(seed)}"
        f"\n\n## 附录\n\n{appendix}\n"
    )


class Corpus:
    def __init__(self, root):
        self.contents = root / "contents"

    def write(self, uri: str, text: str) -> None:
        path = self.contents / uri
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def delete(self, uri: str) -> None:
        (self.contents / uri).unlink()


@pytest.fixture
def corpus(tmp_path, monkeypatch, database):
    corpus = Corpus(tmp_path)
    for module in (builder, change_detector, document_loader, file_scanner):
        monkeypatch.setattr(module, "CONTENTS_DIR", corpus.contents)
    config = tmp_path / "index_config.yml"
    config.write_text('include:\n  - "*/*/*.md"\n', encoding="utf-8")
    monkeypatch.setattr(file_scanner, "INDEX_CONFIG_FILE", config)
    monkeypatch.setattr(uri_mapping, "URI_MAPPING_FILE", tmp_path / "missing.md")
    monkeypatch.setattr(paths, "LOCK_FILE", tmp_path / ".indexer.lock")
    monkeypatch.setattr(pipeline, "head_commit", lambda: None)
    for i in range(1, 5):
        corpus.write(f"ShuiQianXiaoXi/0001-0100/{i:04d}.md", transcript(f"doc{i}"))
    return corpus


@pytest.fixture
def fake_embed(monkeypatch):
    fake = FakeEmbeddings()
    monkeypatch.setattr(embeddings, "generate_embeddings", fake)
    return fake


def build(**kwargs) -> pipeline.RunResult:
    return pipeline.run_once(sync=False, **kwargs)


def snapshot_rows(query):
    return query(
        "SELECT snapshot_id, schema_name, status, pipeline_fingerprint, stats "
        "FROM rag_meta.snapshots ORDER BY published_at"
    )


def snapshot_schemas(query) -> set[str]:
    rows = query("SELECT nspname FROM pg_namespace WHERE nspname ~ '^rag_s[0-9]'")
    return {r[0] for r in rows}


def status(query) -> tuple:
    return query(
        "SELECT last_result, last_error, consecutive_failures "
        "FROM rag_meta.indexer_status"
    )[0]


# ---------------------------------------------------------------------------


def test_sample_scope_refuses_non_local_database(monkeypatch):
    monkeypatch.setattr(pipeline.settings, "indexer_scope", "sample")
    monkeypatch.setattr(pipeline.settings, "postgres_db", "postgres_db")
    with pytest.raises(RuntimeError, match="local-only"):
        pipeline._validate_scope_safety()


def test_first_build_is_full_then_no_change(corpus, fake_embed, query):
    result = build()
    assert result.result == "published"
    [(sid, schema, st, _fp, stats)] = snapshot_rows(query)
    assert (sid, st) == (result.snapshot_id, "published")
    assert stats["build"] == "full"
    assert stats["transcripts"] == 4
    assert stats["embedded_new"] == stats["chunks"]
    assert query(f'SELECT count(*) FROM "{schema}".transcripts')[0][0] == 4

    calls_before = fake_embed.texts
    assert build().result == "no_change"
    assert fake_embed.texts == calls_before, "no-change run must not embed"
    assert len(snapshot_rows(query)) == 1
    assert status(query) == ("no_change", None, 0)


def test_incremental_build_embeds_only_changed_chunks(corpus, fake_embed, query):
    build()
    [(base_id, base_schema, *_)] = snapshot_rows(query)

    changed = "ShuiQianXiaoXi/0001-0100/0002.md"
    corpus.write(changed, transcript("doc2-edited"))
    corpus.write("ShuiQianXiaoXi/0001-0100/0005.md", transcript("doc5"))
    corpus.delete("ShuiQianXiaoXi/0001-0100/0004.md")
    fake_embed.calls.clear()

    result = build()
    assert result.result == "published"
    rows = snapshot_rows(query)
    _, schema, _, _, stats = rows[-1]
    assert stats["build"] == "incremental"
    assert stats["changes"] == {
        "added": 1,
        "body_modified": 1,
        "source_only": 0,
        "deleted": 1,
    }
    # Phase A only embeds the two reindexed transcripts' chunks; the second
    # call is the 5-sample model check.
    embedded = set(fake_embed.calls[0])
    expected = {
        c.text
        for uri in (changed, "ShuiQianXiaoXi/0001-0100/0005.md")
        for c in chunk_document(document_loader.load_indexable_source(uri).document)
    }
    assert embedded == expected
    assert query(f'SELECT count(*) FROM "{schema}".index_state')[0][0] == 4
    assert not query(
        f"SELECT 1 FROM \"{schema}\".document_chunks WHERE doc_id LIKE '%%0004.md'"
    )
    # Unchanged transcripts were copied from the base with identical vectors.
    same = query(
        f'SELECT count(*) FROM "{schema}".document_chunks n '
        f'JOIN "{base_schema}".document_chunks b USING (chunk_id) '
        "WHERE n.doc_id LIKE '%%0001.md' AND n.embedding = b.embedding"
    )[0][0]
    assert same >= 1
    # The audit log records the per-file actions.
    actions = query(
        "SELECT action_type, count(*) FROM rag_state.file_actions GROUP BY 1"
    )
    assert dict(actions) == {"ADD": 5, "MODIFY": 1, "DELETE": 1}


def test_title_only_change_makes_no_embedding_calls(corpus, fake_embed, query):
    build()
    corpus.write(
        "ShuiQianXiaoXi/0001-0100/0001.md",
        transcript("doc1", title="新标题", appendix="附录也改了"),
    )
    fake_embed.calls.clear()
    result = build()
    assert result.result == "published"
    _, schema, _, _, stats = snapshot_rows(query)[-1]
    assert stats["changes"]["source_only"] == 1
    assert fake_embed.calls == [], "metadata-only builds skip the model check too"
    title = query(
        f'SELECT source_title FROM "{schema}".transcripts '
        "WHERE doc_id = 'ShuiQianXiaoXi/0001-0100/0001.md'"
    )[0][0]
    assert title == "新标题"


def test_chunker_bump_forces_full_build_reusing_vectors(
    corpus, fake_embed, query, monkeypatch
):
    build()
    monkeypatch.setattr(chunker, "CHUNKER_VERSION", chunker.CHUNKER_VERSION + 1)
    fake_embed.calls.clear()
    result = build()
    assert result.result == "published"
    rows = snapshot_rows(query)
    assert rows[0][3] != rows[1][3], "new lineage"
    stats = rows[-1][4]
    assert stats["build"] == "full"
    assert stats["embedded_new"] == 0
    assert stats["embedding_reused"] == stats["chunks"]
    # Only the self-check's sampled re-embedding hit the API.
    assert len(fake_embed.calls) == 1 and len(fake_embed.calls[0]) <= 5


def test_failure_in_phase_b_leaves_nothing_behind(
    corpus, fake_embed, query, monkeypatch
):
    build()
    before = (snapshot_rows(query), snapshot_schemas(query))
    corpus.write("ShuiQianXiaoXi/0001-0100/0003.md", transcript("doc3-edited"))

    def explode(*_args, **_kwargs):
        raise RuntimeError("simulated crash while building HNSW")

    monkeypatch.setattr(builder, "create_hnsw_sql", explode)
    result = build()
    assert result.result == "failed"
    assert (snapshot_rows(query), snapshot_schemas(query)) == before
    assert status(query)[0] == "failed"
    assert status(query)[2] == 1


def test_retiring_the_base_mid_build_abandons_the_build(
    corpus, fake_embed, query, monkeypatch
):
    build()
    [(base_id, *_)] = snapshot_rows(query)
    corpus.write("ShuiQianXiaoXi/0001-0100/0003.md", transcript("doc3-edited"))

    real_self_check = builder.self_check

    def retire_then_check(cursor, target, **kwargs):
        checks = real_self_check(cursor, target, **kwargs)
        conn = psycopg2.connect(**_pg_kwargs(settings.postgres_db))
        try:
            catalog.set_status(conn, base_id, catalog.STATUS_RETIRED)
        finally:
            conn.close()
        return checks

    monkeypatch.setattr(builder, "self_check", retire_then_check)
    result = build()
    assert result.result == "failed"
    assert "retired" in result.error
    rows = snapshot_rows(query)
    assert [(r[0], r[2]) for r in rows] == [(base_id, "retired")]


def test_self_check_rejects_a_model_mismatch(corpus, fake_embed, query, monkeypatch):
    real_publish = builder.publish

    def publish_with_other_model(conn, plan, prepared):
        fake_embed.salt = "a-different-model"  # re-embedding now disagrees
        return real_publish(conn, plan, prepared)

    monkeypatch.setattr(pipeline, "publish", publish_with_other_model)
    result = build()
    assert result.result == "failed"
    assert "re-embedded sample disagrees" in result.error
    assert snapshot_rows(query) == []
    assert snapshot_schemas(query) == set()


def test_self_check_rejects_a_transcript_without_chunks(corpus, fake_embed, query):
    corpus.write(
        "ShuiQianXiaoXi/0001-0100/0009.md", "# 空\n\n**发布日期** 2026-01-01\n"
    )
    result = build()
    assert result.result == "failed"
    assert "have no chunks" in result.error
    assert snapshot_rows(query) == []


def test_disk_precheck_refuses_to_build(
    corpus, fake_embed, query, monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "pgdata_path", str(tmp_path))
    monkeypatch.setattr(settings, "build_min_free_bytes", 1 << 60)
    result = build()
    assert result.result == "failed"
    assert "Disk precheck failed" in result.error
    assert snapshot_rows(query) == []


def test_concurrent_run_is_skipped_without_touching_anything(
    corpus, fake_embed, query, monkeypatch
):
    build()
    before = snapshot_rows(query)
    monkeypatch.setattr(
        pipeline,
        "sync_repository",
        lambda: pytest.fail("a skipped run must not touch the git checkout"),
    )
    with run_lock():
        result = pipeline.run_once(sync=True)
    assert result.result == "skipped_busy"
    assert snapshot_rows(query) == before
    assert status(query)[0] == "skipped_busy"


def test_advisory_lock_makes_a_second_writer_skip(corpus, fake_embed, query, database):
    build()
    corpus.write("ShuiQianXiaoXi/0001-0100/0001.md", transcript("doc1-edited"))
    conn = psycopg2.connect(**_pg_kwargs(database))
    plan = builder.plan_build(conn, full=False, source_commit=None)
    prepared = builder.prepare(conn, plan)
    holder = psycopg2.connect(**_pg_kwargs(database))
    try:
        with holder.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (catalog.ADVISORY_LOCK_KEY,))
        with pytest.raises(builder.BuildBusy):
            builder.publish(conn, plan, prepared)
    finally:
        holder.close()
        conn.close()
    assert len(snapshot_rows(query)) == 1


def test_gc_keeps_two_in_current_lineage_and_respects_pins(
    corpus, fake_embed, query, monkeypatch
):
    monkeypatch.setattr(catalog, "GRACE_PERIOD", timedelta(0))
    ids = []
    for i in range(4):
        corpus.write("ShuiQianXiaoXi/0001-0100/0001.md", transcript(f"doc1-v{i}"))
        ids.append(build().snapshot_id)
        if i == 0:
            conn = psycopg2.connect(**_pg_kwargs(settings.postgres_db))
            catalog.set_pinned(conn, ids[0], True)
            conn.close()
    remaining = [r[0] for r in snapshot_rows(query)]
    assert remaining == [ids[0], ids[2], ids[3]]
    assert len(snapshot_schemas(query)) == 3


def test_gc_lock_timeout_is_retried_later(corpus, fake_embed, query, monkeypatch):
    ids = []
    for i in range(3):
        corpus.write("ShuiQianXiaoXi/0001-0100/0001.md", transcript(f"x{i}"))
        ids.append(build().snapshot_id)
    # Within the grace period nothing is collected yet.
    assert len(snapshot_rows(query)) == 3
    first_schema = snapshot_rows(query)[0][1]

    monkeypatch.setattr(catalog, "GRACE_PERIOD", timedelta(0))
    monkeypatch.setattr(catalog, "GC_LOCK_TIMEOUT", "200ms")
    reader = psycopg2.connect(**_pg_kwargs(settings.postgres_db))
    with reader.cursor() as cur:  # an in-flight query still reading it
        cur.execute(f'SELECT count(*) FROM "{first_schema}".documents')
    conn = psycopg2.connect(**_pg_kwargs(settings.postgres_db))
    assert catalog.collect_garbage(conn) == []
    reader.rollback()
    reader.close()
    assert catalog.collect_garbage(conn) == [ids[0]]
    conn.close()


def test_gc_failure_after_publishing_does_not_fail_the_run(
    corpus, fake_embed, query, monkeypatch
):
    real_gc = catalog.collect_garbage
    calls = []

    def gc_failing_after_publish(conn):
        calls.append(conn)
        if len(calls) == 2:  # the GC that follows the publish
            raise RuntimeError("simulated GC failure")
        return real_gc(conn)

    monkeypatch.setattr(catalog, "collect_garbage", gc_failing_after_publish)
    result = build()
    assert result.result == "published"
    assert len(calls) == 2
    assert status(query) == ("published", None, 0)


LEGACY_DDL = """
CREATE SCHEMA rag;
CREATE TABLE rag.document_chunks (
    id SERIAL PRIMARY KEY, chunk_id VARCHAR(255) UNIQUE NOT NULL,
    doc_id VARCHAR(255) NOT NULL, chunk_index INTEGER NOT NULL, heading TEXT,
    text TEXT NOT NULL, word_count INTEGER NOT NULL DEFAULT 0,
    embedding halfvec(16),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX idx_embedding_hnsw ON rag.document_chunks
    USING hnsw (embedding halfvec_cosine_ops);
CREATE TABLE rag.documents (doc_id VARCHAR(255) PRIMARY KEY,
    title VARCHAR(255) NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE rag.indexing_history (id SERIAL PRIMARY KEY,
    file_path VARCHAR(500) UNIQUE NOT NULL, source_hash VARCHAR(64) NOT NULL,
    body_hash VARCHAR(64) NOT NULL, body_normalization_version SMALLINT NOT NULL,
    indexed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    source_observed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE rag.transcripts (doc_id VARCHAR(500) PRIMARY KEY,
    canonical_title VARCHAR(255) NOT NULL, source_title TEXT NOT NULL,
    channel VARCHAR(100) NOT NULL, publication_date VARCHAR(100),
    body_html TEXT NOT NULL, source_hash VARCHAR(64) NOT NULL,
    projection_version INTEGER NOT NULL DEFAULT 2,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE rag.file_actions (id SERIAL PRIMARY KEY,
    file_path VARCHAR(500) NOT NULL, action_type VARCHAR(16) NOT NULL,
    source_hash VARCHAR(64), body_hash VARCHAR(64),
    run_timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    processed_at TIMESTAMP WITH TIME ZONE);
"""


def _populate_legacy(query, corpus_uris):
    query("CREATE EXTENSION IF NOT EXISTS vector")
    query(LEGACY_DDL)
    for uri in corpus_uris:
        src = document_loader.load_indexable_source(uri)
        for c in chunk_document(src.document):
            vec = "[" + ",".join(map(str, fake_vector(c.text))) + "]"
            query(
                "INSERT INTO rag.document_chunks (chunk_id, doc_id, chunk_index, "
                "heading, text, word_count, embedding) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s::halfvec)",
                (c.id, c.doc_id, c.chunk_index, c.heading, c.text, c.word_count, vec),
            )
        query("INSERT INTO rag.documents VALUES (%s, %s)", (uri, "old title"))
        query(
            "INSERT INTO rag.indexing_history (file_path, source_hash, body_hash, "
            "body_normalization_version) VALUES (%s,%s,%s,%s)",
            (uri, src.source_hash, src.body_hash, src.body_normalization_version),
        )
        query(
            "INSERT INTO rag.transcripts (doc_id, canonical_title, source_title, "
            "channel, body_html, source_hash) VALUES (%s,'t','t','c','<p></p>',%s)",
            (uri, src.source_hash),
        )
        query(
            "INSERT INTO rag.file_actions (file_path, action_type) VALUES (%s,'ADD')",
            (uri,),
        )


def test_adoption_registers_legacy_and_first_build_reuses_every_vector(
    corpus, fake_embed, query
):
    uris = sorted(file_scanner.scan_files())
    _populate_legacy(query, uris)

    result = build()
    assert result.result == "published"
    rows = snapshot_rows(query)
    assert [(r[0], r[1], r[2], r[3]) for r in rows[:1]] == [
        ("legacy", "rag", "published", "legacy")
    ]
    assert query("SELECT to_regclass('rag.index_state') IS NOT NULL")[0][0]
    assert query("SELECT to_regclass('rag.indexing_history') IS NULL")[0][0]
    assert query("SELECT count(*) FROM rag_state.file_actions")[0][0] == len(uris)
    stats = rows[-1][4]
    assert stats["build"] == "full"
    assert stats["reuse_source"] == "legacy"
    assert stats["embedded_new"] == 0
    # Adoption is idempotent across restarts.
    assert build().result == "no_change"
    assert len(snapshot_rows(query)) == 2


def test_empty_legacy_schema_is_not_adopted(corpus, fake_embed, query):
    query("CREATE EXTENSION IF NOT EXISTS vector")
    query(LEGACY_DDL)
    build()
    assert "legacy" not in [r[0] for r in snapshot_rows(query)]


def test_agent_role_is_read_only(corpus, fake_embed, query, monkeypatch, database):
    monkeypatch.setattr(settings, "postgres_agent_password", "agent-test-pw")
    build()
    [(_, schema, *_)] = snapshot_rows(query)
    agent = psycopg2.connect(
        **(_pg_kwargs(database) | {"user": "rag_agent", "password": "agent-test-pw"})
    )
    agent.autocommit = True
    with agent.cursor() as cur:
        cur.execute(f'SELECT count(*) FROM "{schema}".document_chunks')
        assert cur.fetchone()[0] > 0
        cur.execute("SELECT count(*) FROM rag_meta.snapshots")
        for statement in (
            f'DELETE FROM "{schema}".document_chunks',
            f"INSERT INTO \"{schema}\".documents VALUES ('x', 'y')",
            "UPDATE rag_meta.snapshots SET status = 'retired'",
            "DELETE FROM rag_state.file_actions",
            "CREATE TABLE rag_meta.evil (i int)",
            f'DROP SCHEMA "{schema}" CASCADE',
        ):
            with pytest.raises(psycopg2.Error):
                cur.execute(statement)
    agent.close()


def test_snapshot_ids_stay_unique_within_one_second(
    corpus, fake_embed, query, monkeypatch
):
    frozen = datetime(2026, 10, 7, 9, 15, 12, tzinfo=UTC)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen

    monkeypatch.setattr(builder, "datetime", FrozenDatetime)
    first = build(full=True).snapshot_id
    second = build(full=True).snapshot_id
    assert first == "s20261007t091512z_0000000"
    assert second == "s20261007t091513z_0000000"
