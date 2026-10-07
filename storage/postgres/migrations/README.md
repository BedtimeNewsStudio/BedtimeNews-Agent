# Historical migrations

These scripts upgraded the pre-snapshot `rag` schema in place (v0.2 → v0.3).
They are kept for reference only and are **no longer needed**:

- A fresh deployment never has a `rag` schema; the indexer builds RAG
  snapshots (`rag_s<id>`) from scratch.
- An existing deployment is adopted automatically when the snapshot-aware
  indexer first starts: its `rag` schema is registered as snapshot `legacy`
  and the first build produces a regular snapshot (see "Upgrading to RAG
  snapshots" in the main README).

If you are upgrading from a release older than v0.3, apply these in order
before starting the new indexer: the adoption expects the v0.3 layout.

No new migrations of this kind will be added: a change to the served tables
is a new snapshot `format_version`, built side by side with the old one.
