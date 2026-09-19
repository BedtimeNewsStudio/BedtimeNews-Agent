"""Incremental RAG indexing and transcript projection pipeline."""

import logging

from .change_detector import ChangeSet, detect_changes, get_doc_id
from .chunker import chunk_document
from .embeddings import generate_embeddings
from .file_scanner import scan_files
from .git_sync import sync_repository
from .models import Chunk
from .paths import CONTENTS_DIR
from .settings import settings
from .stats import collect_stats
from .transcript_export import TRANSCRIPT_PROJECTION_VERSION, project_transcript
from .uri_mapping import load_uri_titles, resolve_title
from .vector_db import (
    close_connection_pool,
    delete_indexed_document,
    delete_transcript_projection,
    get_transcript_states,
    record_source_only_change,
    replace_document_index,
    sync_transcript_titles,
    upsert_document_titles,
    upsert_transcript_projection,
)

logging.basicConfig(
    level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)


def _validate_scope_safety() -> None:
    """Fail closed before a partial scan can touch a production database."""
    if settings.indexer_scope not in {"full", "sample"}:
        raise ValueError("INDEXER_SCOPE must be 'full' or 'sample'")
    if settings.indexer_scope == "sample" and not settings.postgres_db.endswith(
        "_local"
    ):
        raise RuntimeError(
            "Sample indexing is local-only: POSTGRES_DB must end with '_local'"
        )


def main():
    """Synchronize Markdown, RAG state, titles, and reader projections."""
    try:
        logger.info("=" * 70)
        logger.info(" CONTENT INDEXING PIPELINE")
        logger.info("=" * 70)

        _validate_scope_safety()
        logger.info(f"Index scope: {settings.indexer_scope}")

        logger.info("Phase 1: Detecting source and body changes")
        sync_repository()
        current_files = scan_files()
        changes = detect_changes(current_files)
        logger.info(
            "Changes: +%d body~%d source~%d legacy~%d -%d",
            len(changes.added),
            len(changes.body_modified),
            len(changes.source_only),
            len(changes.legacy_requires_reindex),
            len(changes.deleted),
        )

        logger.info("Phase 2: Refreshing document titles")
        titles = sync_document_titles(current_files)

        all_chunks: list[Chunk] = []
        sync_errors: list[tuple[str, Exception]] = []
        try:
            if changes.has_changes:
                logger.info("Phase 3: Applying RAG changes")
                process_deletions(changes.deleted)
                all_chunks = process_content_changes(changes)
                process_source_only_changes(changes)
            else:
                logger.info("Phase 3: No RAG changes")
        except Exception as exc:
            logger.exception("RAG synchronization failed; reader sync will still run")
            sync_errors.append(("RAG", exc))

        rendered = removed = 0
        try:
            logger.info("Phase 4: Synchronizing reader projections")
            rendered, removed = sync_transcript_projections(
                current_files,
                changes.current_source_hashes,
                titles,
            )
        except Exception as exc:
            logger.exception("Reader projection synchronization failed")
            sync_errors.append(("reader projection", exc))

        if sync_errors:
            failed = ", ".join(label for label, _ in sync_errors)
            raise RuntimeError(f"{failed} synchronization failed") from sync_errors[0][
                1
            ]

        if all_chunks:
            logger.info("Phase 5: RAG statistics")
            logger.info("-" * 70)
            stats = collect_stats(all_chunks)
            logger.info(f"Total documents:        {stats['total_documents']}")
            logger.info(f"Total chunks:           {stats['total_chunks']}")
            logger.info(f"Total tokens:           {stats['total_tokens']:,}")
            logger.info(f"Avg tokens per chunk:   {stats['avg_tokens_per_chunk']:.1f}")
            logger.info(f"Min tokens:             {stats['min_tokens']}")
            logger.info(f"Max tokens:             {stats['max_tokens']}")
            logger.info(f"Embedding model:        {stats['embedding_model']}")
            logger.info(f"Estimated API calls:    {stats['estimated_api_calls']}")

        if not changes.has_changes and not rendered and not removed:
            logger.info("No source, body, or reader changes.")

        logger.info("=" * 70)
        logger.info(" PIPELINE COMPLETE")
        logger.info("=" * 70)
    except Exception:
        logger.error("=" * 70)
        logger.error(" PIPELINE FAILED")
        logger.error("=" * 70)
        logger.exception("Pipeline error")
        raise
    finally:
        close_connection_pool()


def sync_document_titles(current_files: set[str]) -> dict[str, str]:
    """Write and return URI -> 标准化标题 for every active document."""
    mapped = load_uri_titles()
    titles = {uri: resolve_title(uri, mapped) for uri in sorted(current_files)}
    upsert_document_titles(titles)
    return titles


def process_deletions(deleted_files: set[str]) -> None:
    """Atomically remove deleted files from all RAG tables."""
    if not deleted_files:
        return
    logger.info(f"Processing {len(deleted_files)} deleted files")
    for uri in sorted(deleted_files):
        delete_indexed_document(uri, get_doc_id(uri))


def process_content_changes(changes: ChangeSet) -> list[Chunk]:
    """Embed and atomically replace added/body-changed/legacy documents."""
    files = changes.reindex_files
    if not files:
        return []

    logger.info(
        "Re-indexing %d added + %d body-modified + %d legacy files",
        len(changes.added),
        len(changes.body_modified),
        len(changes.legacy_requires_reindex),
    )
    all_chunks: list[Chunk] = []

    for index, uri in enumerate(sorted(files), start=1):
        source = changes.loaded_sources[uri]
        chunks = chunk_document(source.document)
        embeddings = (
            generate_embeddings([chunk.text for chunk in chunks]) if chunks else []
        )
        action = "ADD" if uri in changes.added else "MODIFY"

        replace_document_index(
            file_path=uri,
            doc_id=get_doc_id(uri),
            chunks=chunks,
            embeddings=embeddings,
            source_hash=source.source_hash,
            body_hash=source.body_hash,
            body_normalization_version=source.body_normalization_version,
            action_type=action,
        )
        all_chunks.extend(chunks)
        logger.info(f"  [{index}/{len(files)}] {uri}: {len(chunks)} chunks ({action})")

    return all_chunks


def process_source_only_changes(changes: ChangeSet) -> None:
    """Advance source state for title/date/appendix-only edits, without RAG work."""
    if not changes.source_only:
        return
    logger.info(
        "Recording %d source-only changes (zero embedding calls)",
        len(changes.source_only),
    )
    for uri in sorted(changes.source_only):
        source = changes.loaded_sources[uri]
        record_source_only_change(
            file_path=uri,
            source_hash=source.source_hash,
            body_hash=source.body_hash,
            body_normalization_version=source.body_normalization_version,
        )


def sync_transcript_projections(
    current_files: set[str],
    current_source_hashes: dict[str, str],
    titles: dict[str, str],
) -> tuple[int, int]:
    """Apply source add/modify/delete independently of embedding history."""
    stored_states = get_transcript_states()
    removed = sorted(set(stored_states) - current_files)
    changed = sorted(
        uri
        for uri in current_files
        if stored_states.get(uri)
        != (current_source_hashes[uri], TRANSCRIPT_PROJECTION_VERSION)
    )

    for uri in removed:
        delete_transcript_projection(uri)

    for index, uri in enumerate(changed, start=1):
        raw_bytes = (CONTENTS_DIR / uri).read_bytes()
        projection = project_transcript(uri, raw_bytes, titles[uri])
        expected_hash = current_source_hashes[uri]
        if projection.source_hash != expected_hash:
            raise RuntimeError(f"Transcript changed during pipeline run: {uri}")
        upsert_transcript_projection(projection)
        logger.info(f"  [{index}/{len(changed)}] rendered {uri}")

    # URI映射.md is outside contents/, so title-only mapping corrections have no
    # transcript source hash. Refresh the lightweight title field separately.
    sync_transcript_titles(titles)
    logger.info(
        "Reader projections: rendered=%d removed=%d unchanged=%d",
        len(changed),
        len(removed),
        len(current_files) - len(changed),
    )
    return len(changed), len(removed)


if __name__ == "__main__":
    main()
