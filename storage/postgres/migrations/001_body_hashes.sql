-- Upgrade an existing v0.2 indexing history to explicit source/body hashes.
-- Safe to run repeatedly. Existing `content_hash` values are raw source hashes;
-- `body_hash IS NULL` marks those rows as legacy until they are rebuilt.
BEGIN;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'rag' AND table_name = 'indexing_history'
          AND column_name = 'content_hash'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'rag' AND table_name = 'indexing_history'
          AND column_name = 'source_hash'
    ) THEN
        ALTER TABLE rag.indexing_history RENAME COLUMN content_hash TO source_hash;
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'rag' AND table_name = 'indexing_history'
          AND column_name = 'last_modified'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'rag' AND table_name = 'indexing_history'
          AND column_name = 'source_observed_at'
    ) THEN
        ALTER TABLE rag.indexing_history RENAME COLUMN last_modified TO source_observed_at;
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'rag' AND table_name = 'file_actions'
          AND column_name = 'content_hash'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'rag' AND table_name = 'file_actions'
          AND column_name = 'source_hash'
    ) THEN
        ALTER TABLE rag.file_actions RENAME COLUMN content_hash TO source_hash;
    END IF;
END $$;

ALTER TABLE rag.indexing_history
    ADD COLUMN IF NOT EXISTS source_hash VARCHAR(64),
    ADD COLUMN IF NOT EXISTS body_hash VARCHAR(64),
    ADD COLUMN IF NOT EXISTS body_normalization_version SMALLINT,
    ADD COLUMN IF NOT EXISTS source_observed_at TIMESTAMP WITH TIME ZONE
        DEFAULT CURRENT_TIMESTAMP;

ALTER TABLE rag.file_actions
    ADD COLUMN IF NOT EXISTS source_hash VARCHAR(64),
    ADD COLUMN IF NOT EXISTS body_hash VARCHAR(64);
ALTER TABLE rag.file_actions ALTER COLUMN action_type TYPE VARCHAR(16);
ALTER TABLE rag.file_actions DROP CONSTRAINT IF EXISTS valid_action_type;
ALTER TABLE rag.file_actions ADD CONSTRAINT valid_action_type CHECK (
    action_type IN ('ADD', 'MODIFY', 'SOURCE_ONLY', 'DELETE')
);

DROP INDEX IF EXISTS rag.idx_indexing_history_content_hash;
CREATE INDEX IF NOT EXISTS idx_indexing_history_source_hash
    ON rag.indexing_history(source_hash);
CREATE INDEX IF NOT EXISTS idx_indexing_history_body_hash
    ON rag.indexing_history(body_hash);

COMMIT;
