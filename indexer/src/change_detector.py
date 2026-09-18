"""Change detection for transcript sources and their indexable bodies."""

import hashlib
from dataclasses import dataclass, field

from .document_loader import BODY_NORMALIZATION_VERSION, load_indexable_source
from .models import LoadedIndexableSource
from .paths import CONTENTS_DIR
from .vector_db import get_indexing_histories


@dataclass
class ChangeSet:
    """Files requiring each kind of durable state transition."""

    added: set[str] = field(default_factory=set)
    body_modified: set[str] = field(default_factory=set)
    source_only: set[str] = field(default_factory=set)
    legacy_requires_reindex: set[str] = field(default_factory=set)
    deleted: set[str] = field(default_factory=set)
    loaded_sources: dict[str, LoadedIndexableSource] = field(default_factory=dict)

    @property
    def has_changes(self) -> bool:
        return any(
            (
                self.added,
                self.body_modified,
                self.source_only,
                self.legacy_requires_reindex,
                self.deleted,
            )
        )

    @property
    def reindex_files(self) -> set[str]:
        return self.added | self.body_modified | self.legacy_requires_reindex


def detect_changes(current_files: set[str]) -> ChangeSet:
    """Compare synchronized sources with the state represented in PostgreSQL.

    The full-source hash is the cheap first gate. Only a new, changed, or legacy
    source is parsed and normalized. This keeps the hourly no-op path to one raw
    file read per transcript while ensuring only changes to the exact text sent
    to chunking and embeddings cause a RAG rebuild.
    """
    histories = get_indexing_histories()
    changes = ChangeSet(deleted=set(histories) - current_files)

    for uri in sorted(current_files):
        raw_bytes = (CONTENTS_DIR / uri).read_bytes()
        source_hash = hashlib.sha256(raw_bytes).hexdigest()
        history = histories.get(uri)

        if history is None:
            source = load_indexable_source(uri, raw_bytes)
            changes.added.add(uri)
            changes.loaded_sources[uri] = source
            continue

        stored_body_hash = history.get("body_hash")
        stored_version = history.get("body_normalization_version")
        is_legacy = stored_body_hash is None or stored_version is None
        version_changed = stored_version != BODY_NORMALIZATION_VERSION
        source_changed = history.get("source_hash") != source_hash

        if not source_changed and not is_legacy and not version_changed:
            continue

        source = load_indexable_source(uri, raw_bytes)
        changes.loaded_sources[uri] = source

        if is_legacy:
            changes.legacy_requires_reindex.add(uri)
        elif version_changed or stored_body_hash != source.body_hash:
            changes.body_modified.add(uri)
        else:
            changes.source_only.add(uri)

    return changes


def calculate_source_hash(uri: str) -> str:
    """Return the SHA-256 of the complete raw Markdown source."""
    return hashlib.sha256((CONTENTS_DIR / uri).read_bytes()).hexdigest()


def calculate_body_hash(uri: str) -> str:
    """Return the SHA-256 of the exact normalized body sent to chunking."""
    return load_indexable_source(uri).body_hash


def get_doc_id(md_file: str) -> str:
    """Return the URI verbatim; the URI is the document ID."""
    return md_file
