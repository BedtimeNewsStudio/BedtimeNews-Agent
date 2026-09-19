"""Static contract for the idempotent existing-volume migration."""

from pathlib import Path


def test_body_hash_migration_is_idempotent_and_explicit():
    sql = (
        Path(__file__).parents[2] / "storage/postgres/migrations/001_body_hashes.sql"
    ).read_text()

    assert "ADD COLUMN IF NOT EXISTS body_hash" in sql
    assert "ADD COLUMN IF NOT EXISTS body_normalization_version" in sql
    assert "RENAME COLUMN content_hash TO source_hash" in sql
    assert "SOURCE_ONLY" in sql
    assert "DROP CONSTRAINT IF EXISTS valid_action_type" in sql


def test_transcript_projection_migration_invalidates_legacy_renderer_rows():
    sql = (
        Path(__file__).parents[2]
        / "storage/postgres/migrations/002_transcript_projection.sql"
    ).read_text()

    assert "ADD COLUMN IF NOT EXISTS projection_version" in sql
    assert "SET projection_version = 0" in sql
    assert "ALTER COLUMN projection_version SET NOT NULL" in sql
